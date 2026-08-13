from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.security.hmac import hmac_sha256_base64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _canonical(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign_rental_message(payload: dict[str, Any], secret: str) -> str:
    return hmac_sha256_base64(secret, _canonical(payload))


def _canonical_rental_id(value: Any) -> str:
    raw = str(value or "")
    parsed = uuid.UUID(raw)
    canonical = str(parsed)
    if raw.lower() != canonical:
        raise ValueError("rental_id invalido")
    return canonical


class RentalOfflineService:
    def __init__(
        self,
        *,
        mqtt_client: Any,
        device_id: str,
        device_secret: str,
        topic_for: Callable[[str], str],
        root_dir: Path,
        upload_callback: Callable[[str], dict[str, int]],
        logger: Any,
        quarantine_ttl_hours: int = 48,
    ) -> None:
        self.mqtt_client = mqtt_client
        self.device_id = device_id
        self.device_secret = device_secret
        self.topic_for = topic_for
        self.root_dir = root_dir
        self.quarantine_dir = root_dir / "quarantine"
        self.manifest_path = root_dir / "schedule.manifest.json"
        self.upload_callback = upload_callback
        self.logger = logger
        self.quarantine_ttl = timedelta(hours=max(1, quarantine_ttl_hours))
        self._manifest: dict[str, Any] = {"rentals": []}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def start(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._load_manifest()
        self.mqtt_client.subscribe(
            self.topic_for("rental/schedule/desired"), self._handle_schedule
        )
        self.mqtt_client.subscribe(
            self.topic_for("rental/clips/request"), self._handle_clip_request
        )
        self.mqtt_client.add_on_connect_listener(self._request_schedule)
        self._thread = threading.Thread(
            target=self._maintenance_loop,
            daemon=True,
            name="rental-offline-maintenance",
        )
        self._thread.start()
        if self.mqtt_client.is_connected:
            self._request_schedule()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def destination_for(self, meta: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        captured_at = _parse_time(meta.get("created_at")) or _utcnow()
        rental = self._find_rental(captured_at)
        if rental:
            delete_after = rental.get("upload_grace_until")
            rental_id = _canonical_rental_id(rental["rental_id"])
            destination = self.root_dir / rental_id
        else:
            delete_after = (captured_at + self.quarantine_ttl).isoformat()
            destination = self.quarantine_dir
            rental_id = None
        return destination, {
            "rental_id": rental_id,
            "captured_at": captured_at.isoformat(),
            "delete_after": delete_after,
            "offline_state": "pending_manual_upload",
        }

    def inventory(self, rental_id: str) -> dict[str, Any]:
        rental_id = _canonical_rental_id(rental_id)
        self.cleanup_expired()
        directory = self.root_dir / rental_id
        clips: list[dict[str, Any]] = []
        for video in sorted(directory.glob("*.mp4")) if directory.exists() else []:
            sidecar = video.with_suffix(".json")
            try:
                meta = json.loads(sidecar.read_text()) if sidecar.exists() else {}
            except Exception:
                meta = {}
            clips.append(
                {
                    "clip_key": hashlib.sha256(video.name.encode()).hexdigest()[:16],
                    "captured_at": meta.get("created_at") or meta.get("captured_at"),
                    "size_bytes": video.stat().st_size,
                    "delete_after": meta.get("delete_after"),
                }
            )
        return {
            "rental_id": rental_id,
            "pending_count": len(clips),
            "pending_bytes": sum(int(clip["size_bytes"]) for clip in clips),
            "clips": clips,
        }

    def cleanup_expired(self) -> int:
        removed = 0
        now = _utcnow()
        with self._lock:
            for sidecar in self.root_dir.glob("**/*.json"):
                if sidecar == self.manifest_path:
                    continue
                try:
                    meta = json.loads(sidecar.read_text())
                except Exception:
                    meta = {}
                deadline = _parse_time(meta.get("delete_after"))
                if deadline is None and sidecar.parent == self.quarantine_dir:
                    deadline = datetime.fromtimestamp(
                        sidecar.stat().st_mtime, tz=timezone.utc
                    ) + self.quarantine_ttl
                if deadline is None or now <= deadline:
                    continue
                video = sidecar.with_suffix(".mp4")
                video.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                removed += 1
            for directory in self.root_dir.iterdir() if self.root_dir.exists() else []:
                if directory.is_dir() and directory != self.quarantine_dir:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
        if removed:
            self.logger.info("Rental offline: %s clipe(s) expirado(s) removido(s)", removed)
        return removed

    def reconcile_quarantine(self) -> None:
        with self._lock:
            for video in list(self.quarantine_dir.glob("*.mp4")):
                sidecar = video.with_suffix(".json")
                try:
                    meta = json.loads(sidecar.read_text())
                except Exception:
                    meta = {}
                captured_at = _parse_time(meta.get("created_at") or meta.get("captured_at"))
                rental = self._find_rental(captured_at) if captured_at else None
                if not rental:
                    continue
                rental_id = _canonical_rental_id(rental["rental_id"])
                destination = self.root_dir / rental_id
                destination.mkdir(parents=True, exist_ok=True)
                meta.update(
                    {
                        "rental_id": rental_id,
                        "delete_after": rental["upload_grace_until"],
                    }
                )
                sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                shutil.move(str(video), str(destination / video.name))
                shutil.move(str(sidecar), str(destination / sidecar.name))

    def _find_rental(self, captured_at: datetime) -> dict[str, Any] | None:
        for rental in self._manifest.get("rentals", []):
            starts_at = _parse_time(rental.get("starts_at"))
            ends_at = _parse_time(rental.get("ends_at"))
            if starts_at and ends_at and starts_at <= captured_at <= ends_at:
                return rental
        return None

    def _load_manifest(self) -> None:
        try:
            loaded = json.loads(self.manifest_path.read_text())
            if isinstance(loaded.get("rentals"), list):
                self._manifest = loaded
        except Exception:
            return

    def _request_schedule(self) -> None:
        payload = self._signed(
            {
                "type": "rental.schedule.request",
                "device_id": self.device_id,
                "request_id": str(uuid.uuid4()),
                "issued_at": _utcnow().isoformat(),
            }
        )
        self.mqtt_client.publish_json(
            self.topic_for("rental/schedule/request"), payload, qos=1
        )

    def _handle_schedule(self, _topic: str, raw_payload: bytes) -> None:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
            self._validate(payload, "rental.schedule.desired")
            if not isinstance(payload.get("rentals"), list):
                raise ValueError("rentals deve ser lista")
            for rental in payload["rentals"]:
                _canonical_rental_id(rental.get("rental_id"))
            cancelled_ids = [
                _canonical_rental_id(rental_id)
                for rental_id in payload.get("cancelled_rental_ids", [])
            ]
            manifest = {
                "version": payload.get("version"),
                "issued_at": payload.get("issued_at"),
                "rentals": payload["rentals"],
                "cancelled_rental_ids": cancelled_ids,
            }
            temporary = self.manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            os.replace(temporary, self.manifest_path)
            self._manifest = manifest
            self._delete_cancelled(manifest["cancelled_rental_ids"])
            self.reconcile_quarantine()
            self.cleanup_expired()
            self._publish_report(
                "rental/schedule/reported",
                {
                    "type": "rental.schedule.reported",
                    "request_id": payload.get("request_id"),
                    "status": "applied",
                    "rental_count": len(payload["rentals"]),
                },
            )
        except Exception as exc:
            self.logger.warning("Manifesto rental rejeitado: %s", exc)

    def _delete_cancelled(self, rental_ids: list[Any]) -> None:
        for rental_id in rental_ids:
            rental_dir = self.root_dir / str(rental_id)
            if rental_dir.is_dir() and rental_dir.parent == self.root_dir:
                shutil.rmtree(rental_dir)

    def _handle_clip_request(self, _topic: str, raw_payload: bytes) -> None:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
            self._validate(payload, "rental.clips.request")
            rental_id = _canonical_rental_id(payload.get("rental_id"))
            action = str(payload.get("action") or "inventory")
            inventory = self.inventory(rental_id)
            result: dict[str, Any] = inventory
            if action == "upload":
                result = {**inventory, **self.upload_callback(rental_id)}
                result.update(self.inventory(rental_id))
            elif action != "inventory":
                raise ValueError("acao invalida")
            self._publish_report(
                "rental/clips/reported",
                {
                    "type": "rental.clips.reported",
                    "request_id": payload.get("request_id"),
                    "action": action,
                    "status": "completed",
                    **result,
                },
            )
        except Exception as exc:
            self.logger.warning("Solicitacao rental rejeitada: %s", exc)

    def _validate(self, payload: dict[str, Any], expected_type: str) -> None:
        if payload.get("type") != expected_type or payload.get("device_id") != self.device_id:
            raise ValueError("envelope divergente")
        expires_at = _parse_time(payload.get("expires_at"))
        if expires_at is not None and _utcnow() > expires_at:
            raise ValueError("envelope expirado")
        received = str(payload.get("signature") or "")
        expected = sign_rental_message(payload, self.device_secret)
        if not received or not hmac.compare_digest(received, expected):
            raise ValueError("assinatura invalida")

    def _signed(self, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = {**payload, "signature_version": "hmac-sha256-v1"}
        return {**envelope, "signature": sign_rental_message(envelope, self.device_secret)}

    def _publish_report(self, suffix: str, payload: dict[str, Any]) -> None:
        envelope = self._signed(
            {**payload, "device_id": self.device_id, "reported_at": _utcnow().isoformat()}
        )
        self.mqtt_client.publish_json(self.topic_for(suffix), envelope, qos=1)

    def _maintenance_loop(self) -> None:
        while not self._stop.wait(60.0):
            try:
                self.cleanup_expired()
            except Exception as exc:
                self.logger.warning("Falha na limpeza rental offline: %s", exc)
