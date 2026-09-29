"""Signed commands, durable host admission and application-acknowledged results."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.infrastructure.filesystem.deferred_repository import LockBusy, exclusive_file
from src.security.durable_state import durable_unlink
from src.security.durable_state import private_json as atomic_json
from src.security.env_control import canonical_json, request_hash, timestamp
from src.services.mqtt.command_executor import CommandExecutor
from src.services.mqtt.command_policy import CommandPolicy
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger


class CommandDispatcher:
    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        device_id: str,
        command_in_topic: str,
        command_out_topic: str,
        device_secret: str = "",
        policy: CommandPolicy | None = None,
        executor: CommandExecutor | None = None,
        ledger_path: Path | None = None,
        result_path: Path | None = None,
    ):
        self.mqtt_client, self.device_id, self.device_secret = mqtt_client, device_id, device_secret
        self.command_in_topic, self.command_out_topic = command_in_topic, command_out_topic
        self.ack_topic = command_out_topic.rsplit("/", 1)[0] + "/ack"
        self.policy, self.executor = policy or CommandPolicy(), executor or CommandExecutor()
        runtime = Path(os.getenv("GN_RUNTIME_CONFIG_DIR", "/usr/src/app/runtime_config"))
        self.ledger_path = ledger_path or runtime / "device-operation-ledger.json"
        self.result_path = result_path or runtime / "docker-action.last.json"
        self.root = self.ledger_path.parent / "device-operations"
        self.outbox = self.root / "outbox"
        action_service = getattr(self.executor, "action_service", None)
        self.action_root = (
            action_service.action_root
            if action_service
            else self.result_path.parent / "device-actions"
        )
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._sent = {}

    def start(self) -> bool:
        if not self.mqtt_client.is_enabled or not self.device_secret:
            return False
        ok = self.mqtt_client.subscribe(self.command_in_topic, self._handle_message)
        if ok:
            self.mqtt_client.subscribe(self.ack_topic, self._handle_ack)
            self._thread = threading.Thread(
                target=self._report_results, daemon=True, name="device-operation-reporter"
            )
            self._thread.start()
        return ok

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def _sign(self, payload: dict[str, Any]) -> str:
        digest = hmac.new(
            self.device_secret.encode(), canonical_json(payload).encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode()

    def _publish(self, response: dict[str, Any]) -> None:
        self.mqtt_client.publish_json(self.command_out_topic, response, retain=False, qos=1)
        self._sent[response["report_id"]] = time.monotonic()

    def _save(self, path: Path, value: dict[str, Any]) -> None:
        atomic_json(path, value)
        path.chmod(0o600)

    def _record_report(self, path: Path, record: dict[str, Any], response: dict[str, Any]) -> None:
        unsigned = {
            **response,
            "type": "device.operation.report",
            "report_id": str(uuid.uuid4()),
            "reported_at": datetime.now(UTC).isoformat(),
            "signature_version": "hmac-sha256-v1",
        }
        report = {**unsigned, "signature": self._sign(unsigned)}
        record.update(phase="reported", report=report, acknowledged=False)
        # If interrupted before outbox creation, reconciliation restores it from ledger.
        self._save(path, record)
        self._save(self.outbox / f"{report['report_id']}.json", report)
        self._publish(report)

    def _decrypt_wifi(self, parameters: dict[str, Any]) -> dict[str, Any]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        encrypted = parameters.get("password_encrypted")
        if not isinstance(encrypted, dict) or encrypted.get("algorithm") != "aes-256-gcm":
            raise ValueError("invalid encrypted Wi-Fi secret")
        key = hashlib.sha256(f"grn-device-wifi-v1:{self.device_secret}".encode()).digest()
        raw = AESGCM(key).decrypt(
            base64.b64decode(encrypted["iv"]),
            base64.b64decode(encrypted["ciphertext"]) + base64.b64decode(encrypted["tag"]),
            None,
        )
        return {"ssid": parameters.get("ssid"), "wifi_password": raw.decode()}

    def _handle_message(self, topic: str, raw_payload: bytes) -> None:
        try:
            if topic != self.command_in_topic or len(raw_payload) > 65536 or not self.device_secret:
                return
            payload = json.loads(raw_payload)
            signature = payload.pop("signature")
            if (
                payload.get("device_id") != self.device_id
                or payload.get("type") != "device.operation.request"
                or payload.get("signature_version") != "hmac-sha256-v1"
            ):
                raise ValueError("invalid identity")
            if not isinstance(signature, str) or not hmac.compare_digest(
                signature, self._sign(payload)
            ):
                raise ValueError("invalid signature")
            request_id, command = payload["request_id"], payload["command"]
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError("invalid request id")
            with self._lock, exclusive_file(self.root / "ledger.lock"):
                path = self.root / f"{request_id}.json"
                digest = request_hash(payload)
                if path.exists():
                    record = json.loads(path.read_text())
                    if record["request_hash"] != digest:
                        return
                    self._recover_record(path, record)
                    self._publish(record["report"])
                    return
                # Existing legacy IDs must never be executed again after migration.
                legacy = (
                    json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}
                )
                record = {
                    "request_id": request_id,
                    "command": command,
                    "request_hash": digest,
                    "phase": "prepared",
                }
                allowed, reason = self.policy.is_allowed(command, payload)
                if request_id in legacy:
                    allowed, reason = False, "legacy result requires reconciliation"
                if not allowed:
                    self._record_report(
                        path,
                        record,
                        {
                            "device_id": self.device_id,
                            "request_id": request_id,
                            "command": command,
                            "status": "expired" if reason == "command expired" else "failed",
                            "error_code": reason.replace(" ", "_"),
                        },
                    )
                    return
                if command == "change_wifi":
                    payload["parameters"] = self._decrypt_wifi(payload.get("parameters") or {})
                self._save(path, record)
                response = self.executor.execute(command, payload)
                self._record_report(path, record, response)
        except Exception:
            mqtt_logger.warning("Invalid or interrupted remote command; recovery state retained")

    def _recover_record(self, path: Path, record: dict[str, Any]) -> None:
        if record.get("phase") == "prepared":
            known = any(
                (self.action_root / folder / path.name).exists()
                for folder in ("requests", "processing", "results", "receipts")
            )
            self._record_report(
                path,
                record,
                {
                    "device_id": self.device_id,
                    "request_id": record["request_id"],
                    "command": record["command"],
                    "status": "accepted" if known else "unknown",
                    "error_code": None
                    if known
                    else "interrupted_admission_requires_reconciliation",
                },
            )
        elif not record.get("acknowledged"):
            report = record["report"]
            if (
                not isinstance(report, dict)
                or not self._uuid(report.get("report_id"))
                or report.get("request_id") != path.stem
                or report.get("device_id") != self.device_id
            ):
                raise ValueError("invalid stored report identity")
            target = self.outbox / f"{report['report_id']}.json"
            if not target.exists():
                self._save(target, report)

    def _handle_ack(self, topic: str, raw_payload: bytes) -> None:
        try:
            if topic != self.ack_topic or len(raw_payload) > 16384 or not self.device_secret:
                return
            payload = json.loads(raw_payload)
            signature = payload.pop("signature")
            if (
                payload.get("type") != "device.operation.ack"
                or payload.get("device_id") != self.device_id
                or payload.get("signature_version") != "hmac-sha256-v1"
                or payload.get("status") != "persisted"
                or not hmac.compare_digest(signature, self._sign(payload))
            ):
                return
            report_id, request_id = payload["report_id"], payload["request_id"]
            if str(uuid.UUID(report_id)) != report_id or str(uuid.UUID(request_id)) != request_id:
                return
            with self._lock, exclusive_file(self.root / "ledger.lock"):
                pending = self.outbox / f"{report_id}.json"
                if (
                    not pending.exists()
                    or json.loads(pending.read_text())["request_id"] != request_id
                ):
                    return
                path = self.root / f"{request_id}.json"
                record = json.loads(path.read_text())
                if record["report"]["report_id"] == report_id:
                    record["acknowledged"] = True
                    self._save(path, record)
                durable_unlink(pending)
                self._sent.pop(report_id, None)
        except (OSError, ValueError, KeyError, TypeError, LockBusy):
            mqtt_logger.warning("Invalid command persistence acknowledgement ignored")

    @staticmethod
    def _object(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid durable record")
        return value

    @staticmethod
    def _uuid(value: Any) -> bool:
        return isinstance(value, str) and str(uuid.UUID(value)) == value

    def _copy_host_result(self, result_path: Path) -> None:
        result = self._object(result_path)
        request_id = result.get("request_id")
        if (
            not self._uuid(request_id)
            or result_path.stem != request_id
            or result.get("schema_version") != 2
            or result.get("action") not in self.policy.allowed_commands | {"shutdown_host"}
            or result.get("status") not in {"ok", "error", "unknown"}
        ):
            return
        timestamp(result.get("completed_at"))
        receipt = self.action_root / "receipts" / result_path.name
        if receipt.exists():
            return
        path = self.root / result_path.name
        record = self._object(path) if path.exists() else None
        if record is not None and result["action"] != record["command"]:
            return
        # Every receipt promises a durable copy, including terminal/unknown commands
        # and env/config/Pico work that has no API operation record.
        self._save(self.root / "host-results" / result_path.name, result)
        if record is not None and record.get("report", {}).get("status") not in {
            "succeeded",
            "failed",
            "expired",
            "unknown",
        }:
            safe = {
                k: result[k] for k in ("action", "status", "completed_at", "stage") if k in result
            }
            self._record_report(
                path,
                record,
                {
                    "device_id": self.device_id,
                    "request_id": request_id,
                    "command": record["command"],
                    "status": {"ok": "succeeded", "error": "failed", "unknown": "unknown"}[
                        result["status"]
                    ],
                    "result": safe,
                    "error_code": result.get("error_code"),
                },
            )
        self._save(
            receipt,
            {
                "schema_version": 2,
                "request_id": request_id,
                "action": result["action"],
                "received_at": datetime.now(UTC).isoformat(),
            },
        )

    def poll_once(self) -> None:
        with self._lock, exclusive_file(self.root / "ledger.lock"):
            for path in self.root.glob("*.json"):
                try:
                    record = self._object(path)
                    if not self._uuid(path.stem) or record.get("request_id") != path.stem:
                        raise ValueError("ledger identity mismatch")
                    self._recover_record(path, record)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    mqtt_logger.warning("Command ledger record retained for reconciliation")
            for result_path in (self.action_root / "results").glob("*.json"):
                try:
                    self._copy_host_result(result_path)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    mqtt_logger.warning("Host result retained for reconciliation")
            for path in self.outbox.glob("*.json"):
                try:
                    report = self._object(path)
                    if (
                        not self._uuid(report.get("report_id"))
                        or path.stem != report["report_id"]
                        or not self._uuid(report.get("request_id"))
                    ):
                        raise ValueError("outbox identity mismatch")
                    if time.monotonic() - self._sent.get(report["report_id"], -30) >= 30:
                        self._publish(report)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    mqtt_logger.warning("Command outbox record retained for reconciliation")

    def _report_results(self) -> None:
        while not self._stop.wait(2):
            try:
                self.poll_once()
            except (OSError, ValueError, KeyError, TypeError, LockBusy):
                mqtt_logger.warning("Command result reconciliation pending")
