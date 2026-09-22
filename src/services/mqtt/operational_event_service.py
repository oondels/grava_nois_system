"""Durable operational outbox. MQTT acceptance is never a persistence receipt.

The capture/events v2 and state v2 ACK extensions require backend support.
An older backend leaves these messages pending, including after reconnection.
"""

import hashlib
import hmac
import json
import random
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from src.infrastructure.filesystem.deferred_repository import atomic_json, safe_child
from src.security.hmac import hmac_sha256_base64
from src.utils.logger import logger


def content_hash(payload: dict) -> str:
    content = {k: v for k, v in payload.items() if k not in {"signature", "content_hash"}}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def sign_operational(payload: dict, secret: str) -> str:
    return hmac_sha256_base64(secret, "v2:OPERATIONAL:" + content_hash(payload))


class OperationalEventService:
    OUTBOX_LIMIT = 64 * 1024 * 1024
    CRITICAL_RESERVE = 8 * 1024 * 1024
    HISTORY_LIMIT = 32 * 1024 * 1024

    def __init__(self, root, mqtt_client, topic_for, identity, secret, *, monotonic=time.monotonic):
        self.root = Path(root)
        self.outbox = self.root / "outbox"
        self.history = self.root / "history"
        self.client = mqtt_client
        self.topic_for = topic_for
        self.identity = dict(identity)
        self.secret = secret
        self.clock = monotonic
        self._lock = threading.RLock()
        self._last_send = -1.0
        self.last_ack_at = None
        self._retry = {}
        self._recent = {}
        self._stop = threading.Event()
        self._thread = None
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)
        self.status_path = self.root / "status.json"
        try:
            self.status = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            self.status = {"sequence": 0, "suppressed": 0, "saturated": False}
        self.last_ack_at = self.status.get("last_ack_at")

    def start(self):
        self.client.subscribe(self.topic_for("capture/events/ack"), self._ack, qos=1)
        self.client.subscribe(self.topic_for("state/ack"), self._ack, qos=1)
        self._thread = threading.Thread(target=self._loop, name="operational-outbox", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def _sequence(self):
        self.status["sequence"] += 1
        atomic_json(self.status_path, self.status)
        return self.status["sequence"]

    def emit(
        self,
        event_type,
        *,
        job=None,
        stage="runtime",
        code="unknown",
        severity="warning",
        situation="open",
        incident_id=None,
        impact=None,
        context=None,
    ):
        with self._lock:
            try:
                incident = incident_id or f"{job.job_id if job else 'device'}:{stage}"
                fingerprint = (incident, code, situation)
                previous = self._recent.get(fingerprint)
                if previous is not None and self.clock() - previous < 60:
                    return
                payload = {
                    "schema_version": 2,
                    "type": event_type,
                    **self.identity,
                    "event_id": str(uuid.uuid4()),
                    "sequence": self._sequence(),
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "stage": stage,
                    "code": code,
                    "reason": code,
                    "severity": severity,
                    "situation": situation,
                    "incident_id": incident,
                    "impact": impact or code,
                }
                if context:
                    payload.update(
                        {
                            k: v
                            for k, v in context.items()
                            if k in {"trigger_id", "captured_at", "camera_id"}
                        }
                    )
                if job:
                    payload.update(
                        job_id=job.job_id,
                        camera_id=job.camera_id.value,
                        trigger_id=job.details.get("trigger_id"),
                        captured_at=job.details.get("captured_at"),
                        attempt=job.details.get("attempts_by_stage", {}).get(stage, 0),
                    )
                if self._store(payload, critical=severity == "error" or situation == "resolved"):
                    self._recent[fingerprint] = self.clock()
                    if len(self._recent) > 1024:
                        self._recent.pop(next(iter(self._recent)))
            except (OSError, ValueError):
                logger.error("Operational event could not be persisted: %s", code)

    def _store(self, payload, critical=False):
        payload["content_hash"] = content_hash(payload)
        payload["signature_version"] = "hmac-sha256-v2"
        payload["content_hash"] = content_hash(payload)
        payload["signature"] = sign_operational(payload, self.secret)
        size = len(json.dumps(payload).encode()) + 1024
        used = sum(p.stat().st_size for p in self.outbox.glob("*.json"))
        limit = self.OUTBOX_LIMIT if critical else self.OUTBOX_LIMIT - self.CRITICAL_RESERVE
        if used + size > limit:
            self.status.update(saturated=True, suppressed=self.status["suppressed"] + 1)
            atomic_json(self.status_path, self.status)
            logger.error("Operational outbox saturated; occurrence retained in job when available")
            return False
        atomic_json(self.outbox / f"{payload['event_id']}.json", payload)
        return True

    def snapshot(self, state):
        with self._lock:
            try:
                payload = {
                    "schema_version": 2,
                    "type": "device.operational_state",
                    **self.identity,
                    "event_id": "state",
                    "sequence": self._sequence(),
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "operational": state,
                    "telemetry": {**self.status, "last_ack_at": self.last_ack_at},
                    "signature_version": "hmac-sha256-v2",
                }
                payload["content_hash"] = content_hash(payload)
                payload["signature"] = sign_operational(payload, self.secret)
                atomic_json(self.root / "state.json", payload)
                self._retry.pop("state", None)
            except OSError:
                logger.error("Operational snapshot could not be persisted")

    def _ack(self, topic, raw):
        try:
            payload = json.loads(raw)
            if not self.secret or any(payload.get(k) != v for k, v in self.identity.items()):
                return
            if not hmac.compare_digest(
                str(payload.get("signature", "")), sign_operational(payload, self.secret)
            ):
                return
            event_id = payload.get("event_id")
            if event_id == "state":
                if topic != self.topic_for("state/ack"):
                    return
                path = self.root / "state.json"
            else:
                if (
                    topic != self.topic_for("capture/events/ack")
                    or str(uuid.UUID(event_id)) != event_id
                ):
                    return
                path = safe_child(self.outbox, f"{event_id}.json")
            with self._lock:
                current = json.loads(path.read_text())
                if payload.get("ack_hash") != current["content_hash"] or payload.get(
                    "status"
                ) not in {"persisted", "duplicate"}:
                    return
                atomic_json(self.history / f"{event_id}.json", current)
                path.unlink()
                self._retry.pop(event_id, None)
                self.last_ack_at = datetime.now(UTC).isoformat()
                used = sum(p.stat().st_size for p in self.outbox.glob("*.json"))
                self.status.update(
                    last_ack_at=self.last_ack_at,
                    saturated=used >= self.OUTBOX_LIMIT - self.CRITICAL_RESERVE,
                )
                atomic_json(self.status_path, self.status)
                self._prune_history()
        except (ValueError, TypeError, OSError, KeyError, AttributeError):
            return

    def flush_once(self):
        now = self.clock()
        if not self.secret or now - self._last_send < 1:
            return
        with self._lock:
            paths = sorted(self.outbox.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if (self.root / "state.json").exists():
                paths.insert(0, self.root / "state.json")
            for path in paths:
                try:
                    payload = json.loads(path.read_text())
                    key = payload["event_id"]
                    attempt, due = self._retry.get(key, (0, 0))
                    if now < due:
                        continue
                    topic = "state" if key == "state" else "capture/events"
                    self.client.publish_json(self.topic_for(topic), payload, qos=1, retain=False)
                    # Keep file even on successful publish; only backend ACK removes it.
                    self._retry[key] = (
                        attempt + 1,
                        now + min(900, 30 * 2 ** min(attempt, 5)) * random.uniform(0.8, 1.2),
                    )
                    self._last_send = now
                    break
                except (OSError, ValueError, KeyError):
                    logger.error("Invalid operational outbox entry retained")

    def _prune_history(self):
        files = sorted(self.history.glob("*.json"), key=lambda p: p.stat().st_mtime)
        used = sum(p.stat().st_size for p in files)
        for path in files:
            if used <= self.HISTORY_LIMIT and time.time() - path.stat().st_mtime <= 7 * 86400:
                continue
            used -= path.stat().st_size
            path.unlink()

    def _loop(self):
        while not self._stop.wait(1):
            try:
                self.flush_once()
            except Exception:
                logger.error("Operational outbox iteration failed; pending entries retained")
