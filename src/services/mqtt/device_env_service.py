"""Authenticated, replay-safe administrative .env control over MQTT v2."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config.config_loader import configuration_transaction
from src.infrastructure.filesystem.deferred_repository import LockBusy, exclusive_file
from src.security.durable_state import private_json as _private_json
from src.security.env_control import request_hash, sign_control, timestamp, validate_control
from src.security.env_envelope import open_env_envelope, seal_env_envelope
from src.services.docker_action_request import DockerActionRequestService
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_env_keys(content: str) -> list[str]:
    return [
        line.split("=", 1)[0].strip()
        for line in content.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]


def _content_hash(content: str) -> str:
    return base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()


class DeviceEnvService:
    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        device_id: str,
        client_id: str | None,
        venue_id: str | None,
        request_topic: str,
        desired_topic: str,
        reported_topic: str,
        env_path: str | Path | None = None,
        device_secret: str = "",
        agent_version: str = "local-dev",
        ledger_dir: Path | None = None,
        action_service=None,
    ):
        self.mqtt_client = mqtt_client
        self.device_id, self.client_id, self.venue_id = device_id, client_id, venue_id
        self.request_topic, self.desired_topic, self.reported_topic = (
            request_topic,
            desired_topic,
            reported_topic,
        )
        self.env_path = Path(
            env_path or os.getenv("GN_HOST_ENV_PATH", "/usr/src/app/host_config/.env")
        )
        self.device_secret, self.agent_version = device_secret, agent_version
        self.ledger_dir = (
            ledger_dir
            or Path(os.getenv("GN_RUNTIME_CONFIG_DIR", "/usr/src/app/runtime_config"))
            / "env-control"
        )
        self.action_service = action_service or DockerActionRequestService.from_env(
            logger=mqtt_logger
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._connect_listener_registered = False

    def start(self) -> bool:
        if not self.mqtt_client.is_enabled or not self.device_secret:
            return False
        if not self._connect_listener_registered:
            self.mqtt_client.add_on_connect_listener(self._handle_mqtt_connect)
            self._connect_listener_registered = True
        self.mqtt_client.subscribe(self.request_topic, self._handle_message)
        self.mqtt_client.subscribe(self.desired_topic, self._handle_message)
        self._thread = threading.Thread(
            target=self._retry_reports, daemon=True, name="env-control-reports"
        )
        self._thread.start()
        if self.mqtt_client.is_connected:
            self._handle_mqtt_connect()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def _retry_reports(self) -> None:
        while not self._stop.wait(30):
            self._handle_mqtt_connect()

    def _handle_mqtt_connect(self) -> None:
        # API correlation lasts 15 minutes. Never replay snapshots after their
        # request expiry: a new sync must observe current data.
        with self._lock:
            for path in self.ledger_dir.glob("*.json"):
                try:
                    record = json.loads(path.read_text())
                    if record.get("phase") == "done" and timestamp(
                        record["expires_at"]
                    ) + timedelta(days=1) < datetime.now(UTC):
                        path.unlink()
                        continue
                    if record.get("type") != "env.desired":
                        continue
                    with (
                        exclusive_file(self.ledger_dir / "control.lock"),
                        configuration_transaction,
                    ):
                        if record.get("phase") != "done":
                            self._recover(record, path)
                        if timestamp(record["expires_at"]) + timedelta(minutes=13) > datetime.now(
                            UTC
                        ):
                            self._publish(record["report"])
                except (OSError, ValueError, KeyError, TypeError, AttributeError, LockBusy):
                    mqtt_logger.warning("Env control recovery pending; no automatic reapplication")

    def _handle_message(self, topic: str, raw_payload: bytes) -> None:
        try:
            if len(raw_payload) > 512 * 1024:
                raise ValueError("payload_too_large")
            payload = json.loads(raw_payload)
            expected = {self.request_topic: "env.request", self.desired_topic: "env.desired"}.get(
                topic
            )
            if expected is None:
                raise ValueError("invalid_topic")
            validate_control(self.device_secret, payload, self.device_id, expected)
        except (ValueError, TypeError, KeyError, AttributeError):
            # Unauthenticated requests cannot create authoritative rejection reports.
            mqtt_logger.warning("Invalid administrative env control ignored")
            return
        with self._lock:
            try:
                with exclusive_file(self.ledger_dir / "control.lock"), configuration_transaction:
                    self._process(payload)
            except Exception:
                # Never log raw exceptions/content. No claim of success after IO failure.
                mqtt_logger.error("Administrative env control failed; recovery artifacts retained")
                # A durable intent may already have replaced the file. Publishing a
                # rejection now would conflict with the immutable recovered result.
                path = self.ledger_dir / f"{payload['request_id']}.json"
                if not path.exists():
                    self._publish(
                        self._report(
                            payload["request_id"], "rejected", rejection_reason="env_control_failed"
                        )
                    )

    def _process(self, payload: dict[str, Any]) -> None:
        path = self.ledger_dir / f"{payload['request_id']}.json"
        digest = request_hash(payload)
        if path.exists():
            record = json.loads(path.read_text())
            if record["request_hash"] != digest:
                self._publish(
                    self._report(
                        payload["request_id"], "rejected", rejection_reason="request_id_conflict"
                    )
                )
                return
            if record["phase"] != "done":
                self._recover(record, path)
            self._publish(record["report"])
            return
        if payload["type"] == "env.request":
            content = self._read_env_file()
            report = self._report(
                payload["request_id"],
                "snapshot",
                env_hash=_content_hash(content),
                env_keys=_parse_env_keys(content),
                envelope=seal_env_envelope(
                    self.device_secret, payload["request_id"], self.device_id, content
                ),
            )
            record = {
                "type": payload["type"],
                "request_hash": digest,
                "expires_at": payload["expires_at"],
                "phase": "done",
                "report": report,
            }
            _private_json(path, record)
            self._publish(report)
            return
        if type(payload.get("restart_after_apply")) is not bool:
            raise ValueError("invalid_restart_flag")
        envelope = payload.get("envelope")
        if (
            not isinstance(envelope, dict)
            or envelope.get("device_id") != self.device_id
            or envelope.get("request_id") != payload["request_id"]
        ):
            raise ValueError("envelope_identity_mismatch")
        content = open_env_envelope(self.device_secret, envelope)
        self._validate_env_content(content)
        # Require an existing managed file: a wrong mount must not create a new identity.
        self._read_env_file()
        record = {
            "type": payload["type"],
            "request_id": payload["request_id"],
            "request_hash": digest,
            "expires_at": payload["expires_at"],
            "target_hash": _content_hash(content),
            "env_keys": _parse_env_keys(content),
            "restart_requested": payload["restart_after_apply"],
            "phase": "prepared",
        }
        _private_json(path, record)
        self._create_backup(payload["request_id"])
        self._write_env_atomic(content)
        record["phase"] = "applied"
        _private_json(path, record)
        self._recover(record, path)
        self._publish(record["report"])

    def _recover(self, record: dict[str, Any], path: Path) -> None:
        if _content_hash(self._read_env_file()) != record["target_hash"]:
            report = self._report(
                record["request_id"],
                "rejected",
                rejection_reason="interrupted_apply_requires_sync",
                restart_requested=record["restart_requested"],
                restart_status="uncertain",
            )
        else:
            restart_status, error = "not_requested", None
            if record["restart_requested"]:
                result = self.action_service.submit_action(
                    "restart_container",
                    source="mqtt",
                    request_id=record["request_id"],
                    expires_at=record["expires_at"],
                )
                restart_status = (
                    "queued"
                    if result.accepted
                    else ("uncertain" if result.code == "uncertain" else "rejected")
                )
                error = None if result.accepted else result.code
            report = self._report(
                record["request_id"],
                "applied_requires_restart",
                env_hash=record["target_hash"],
                env_keys=record["env_keys"],
                restart_requested=record["restart_requested"],
                restart_status=restart_status,
            )
            if error:
                report["restart_error_code"] = error
        record.update(phase="done", report=report)
        _private_json(path, record)

    def _report(self, request_id: str, status: str, **fields) -> dict[str, Any]:
        return {
            "type": "env.reported",
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "request_id": request_id,
            "status": status,
            "reported_at": _now_iso(),
            "agent_version": self.agent_version,
            **fields,
        }

    def _publish(self, report: dict[str, Any]) -> bool:
        now = datetime.now(UTC)
        signed = sign_control(
            self.device_secret,
            {
                **report,
                "issued_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=120)).isoformat(),
            },
        )
        return self.mqtt_client.publish_json(self.reported_topic, signed, qos=1, retain=False)

    def _read_env_file(self) -> str:
        if not self.env_path.is_file() or self.env_path.is_symlink():
            raise ValueError("managed_env_unavailable")
        return self.env_path.read_text(encoding="utf-8")

    @staticmethod
    def _validate_env_content(content: str) -> None:
        if "\x00" in content or (content.strip() and not _parse_env_keys(content)):
            raise ValueError("invalid_env_content")

    def _create_backup(self, request_id: str) -> Path:
        backup = self.env_path.with_name(self.env_path.name + ".bak.grn." + request_id)
        if not backup.exists():
            self._write_atomic(backup, self._read_env_file())
        return backup

    def _write_env_atomic(self, content: str) -> None:
        self._write_atomic(self.env_path, content)

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
