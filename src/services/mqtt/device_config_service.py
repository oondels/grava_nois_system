from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.config_loader import get_config_path, reset_config_cache
from src.config.config_schema import validate_config_dict
from src.security.hmac import hmac_sha256_base64
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger

REMOTE_CONFIG_SCHEMA_VERSION = 1
_HASH_PREFIX = "sha256:"
_SIGNATURE_VERSION = "hmac-sha256-v1"

_ALLOWED_TOP_LEVEL_KEYS = {
    "version",
    "updatedAt",
    "capture",
    "cameras",
    "triggers",
    "processing",
    "operationWindow",
    "mqtt",
}

_REQUIRED_REMOTE_CONFIG_KEYS = {
    "capture",
    "cameras",
    "triggers",
    "processing",
    "operationWindow",
    "mqtt",
}

_ALLOWED_KEYS_BY_PATH: dict[tuple[str, ...], set[str]] = {
    (): _ALLOWED_TOP_LEVEL_KEYS,
    ("capture",): {"segmentSeconds", "preSegments", "postSegments", "rtsp", "v4l2"},
    ("capture", "rtsp"): {
        "maxRetries",
        "timeoutSeconds",
        "startupCheckSeconds",
        "reencode",
        "fps",
        "gop",
        "preset",
        "crf",
        "useWallclockTimestamps",
    },
    ("capture", "v4l2"): {"device", "framerate", "videoSize"},
    ("cameras", "*"): {
        "id",
        "name",
        "enabled",
        "sourceType",
        "rtspUrl",
        "picoTriggerToken",
        "preSegments",
        "postSegments",
    },
    ("triggers",): {"source", "maxWorkers", "pico", "gpio"},
    ("triggers", "pico"): {"globalToken"},
    ("triggers", "gpio"): {"pin", "debounceMs", "cooldownSeconds"},
    ("processing",): {
        "lightMode",
        "maxAttempts",
        "mobileFormat",
        "verticalFormat",
        "watermark",
    },
    ("processing", "watermark"): {"preset", "relativeWidth", "opacity", "margin"},
    ("operationWindow",): {"timeZone", "start", "end"},
    ("mqtt",): {
        "enabled",
        "broker",
        "keepaliveSeconds",
        "heartbeatIntervalSeconds",
        "topicPrefix",
        "qos",
        "retainPresence",
    },
    ("mqtt", "broker"): {"host", "port", "tls"},
}

_FORBIDDEN_KEY_MARKERS = {
    "secret",
    "password",
    "passwd",
    "token",
    "username",
    "privatekey",
    "private_key",
    "apikey",
    "api_key",
}

_ALLOWED_TOKEN_PATHS = {
    ("cameras", "*", "picoTriggerToken"),
    ("triggers", "pico", "globalToken"),
}

_RESTART_PATHS = {
    ("capture",),
    ("cameras",),
    ("triggers", "source"),
    ("triggers", "maxWorkers"),
    ("triggers", "pico", "globalToken"),
    ("triggers", "gpio", "pin"),
    ("processing", "lightMode"),
    ("processing", "maxAttempts"),
    ("processing", "mobileFormat"),
    ("processing", "verticalFormat"),
    ("mqtt", "enabled"),
    ("mqtt", "broker"),
    ("mqtt", "keepaliveSeconds"),
    ("mqtt", "topicPrefix"),
    ("mqtt", "qos"),
    ("mqtt", "retainPresence"),
}


class RemoteConfigError(Exception):
    """Erro de validação ou aplicação de configuração remota."""


class DeviceConfigService:
    """Handles desired config messages without enabling arbitrary commands."""

    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        device_id: str,
        client_id: str,
        venue_id: str,
        desired_topic: str,
        reported_topic: str,
        config_path: Path | None = None,
        device_secret: str | None = None,
        agent_version: str = "local-dev",
    ):
        self.mqtt_client = mqtt_client
        self.device_id = device_id
        self.client_id = client_id
        self.venue_id = venue_id
        self.desired_topic = desired_topic
        self.reported_topic = reported_topic
        self.config_path = config_path or get_config_path()
        self.pending_path = self.config_path.with_name("config.pending.json")
        self.backup_path = self.config_path.with_name("config.backup.json")
        self.state_path = self.config_path.with_name("config.state.json")
        self.device_secret = device_secret or ""
        self.agent_version = agent_version

    def start(self) -> bool:
        if not self.mqtt_client.is_enabled:
            return False
        return self.mqtt_client.subscribe(self.desired_topic, self._handle_message)

    def stop(self) -> None:
        return None

    def _handle_message(self, topic: str, raw_payload: bytes) -> None:
        _ = topic
        payload: dict[str, Any]
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RemoteConfigError("payload deve ser um objeto JSON")
            result = self.process_desired_config(payload)
        except Exception as exc:
            result = _ReportResult(
                status="rejected",
                config_version=_safe_int_from_payload(raw_payload),
                correlation_id=None,
                requires_restart=False,
                reported_hash=None,
                rejection_reason=str(exc),
            )
        self.publish_report(result)

    def process_desired_config(self, payload: dict[str, Any]) -> "_ReportResult":
        config_version = _required_int(payload, "config_version")
        correlation_id = _required_str(payload, "correlation_id")
        schema_version = _required_int(payload, "schema_version")
        desired_hash = _required_str(payload, "desired_hash")
        desired_config = payload.get("desired_config")

        if schema_version != REMOTE_CONFIG_SCHEMA_VERSION:
            raise RemoteConfigError(f"schema_version não suportado: {schema_version}")
        if _required_str(payload, "type") != "config.desired":
            raise RemoteConfigError("type inválido para config remota")
        if _required_str(payload, "device_id") != self.device_id:
            raise RemoteConfigError("device_id divergente")
        if _required_str(payload, "client_id") != self.client_id:
            raise RemoteConfigError("client_id divergente")
        if _required_str(payload, "venue_id") != self.venue_id:
            raise RemoteConfigError("venue_id divergente")
        if not isinstance(desired_config, dict):
            raise RemoteConfigError("desired_config deve ser um objeto")
        if _parse_iso_datetime(_required_str(payload, "expires_at")) <= _now_utc():
            raise RemoteConfigError("mensagem de config expirada")

        desired_config = _prepare_config_payload(
            desired_config,
            config_version=config_version,
            updated_at=_required_str(payload, "issued_at"),
        )
        _validate_signature(payload, desired_hash, self.device_secret)
        _validate_hash(desired_config, desired_hash)
        _validate_remote_config(desired_config)

        state = self._load_state()
        last_applied_version = int(state.get("lastAppliedVersion") or 0)
        pending_version = int(state.get("pendingVersion") or 0)
        if config_version <= last_applied_version:
            raise RemoteConfigError("config_version antiga ou já aplicada")
        if pending_version and config_version < pending_version:
            raise RemoteConfigError("config_version anterior à versão pendente")

        current_config = self._load_current_config()
        requires_restart = payload.get("requires_restart")
        if requires_restart is None:
            requires_restart = _requires_restart(current_config, desired_config)
        else:
            requires_restart = bool(requires_restart) or _requires_restart(
                current_config, desired_config
            )

        self._write_pending(desired_config)
        if requires_restart:
            self._write_state(
                {
                    **state,
                    "pendingVersion": config_version,
                    "pendingHash": desired_hash,
                    "pendingCorrelationId": correlation_id,
                    "lastStatus": "pending_restart",
                    "lastUpdatedAt": _now_iso(),
                }
            )
            return _ReportResult(
                status="pending_restart",
                config_version=config_version,
                correlation_id=correlation_id,
                requires_restart=True,
                reported_hash=desired_hash,
                rejection_reason=None,
            )

        self._promote_pending(desired_config)
        self._write_state(
            {
                **state,
                "lastAppliedVersion": config_version,
                "lastAppliedHash": desired_hash,
                "lastAppliedAt": _now_iso(),
                "pendingVersion": None,
                "pendingHash": None,
                "pendingCorrelationId": None,
                "lastStatus": "applied",
                "lastUpdatedAt": _now_iso(),
            }
        )
        return _ReportResult(
            status="applied",
            config_version=config_version,
            correlation_id=correlation_id,
            requires_restart=False,
            reported_hash=desired_hash,
            rejection_reason=None,
        )

    def publish_report(self, result: "_ReportResult") -> bool:
        payload: dict[str, Any] = {
            "type": "config.reported",
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "schema_version": REMOTE_CONFIG_SCHEMA_VERSION,
            "config_version": result.config_version,
            "correlation_id": result.correlation_id,
            "status": result.status,
            "requires_restart": result.requires_restart,
            "reported_hash": result.reported_hash,
            "reported_at": _now_iso(),
            "rejection_reason": _sanitize_reason(result.rejection_reason),
            "agent_version": self.agent_version,
        }
        if self.device_secret:
            payload["signature_version"] = _SIGNATURE_VERSION
            payload["signature"] = sign_reported_config_payload(
                payload=payload,
                device_secret=self.device_secret,
            )
        return self.mqtt_client.publish_json(
            self.reported_topic,
            payload,
            retain=False,
        )

    def _load_current_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_pending(self, config_data: dict[str, Any]) -> None:
        _atomic_write_json(self.pending_path, config_data)

    def _write_state(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def _promote_pending(self, config_data: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            shutil.copy2(self.config_path, self.backup_path)
        _atomic_write_json(self.config_path, config_data)
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass
        reset_config_cache()


class _ReportResult:
    def __init__(
        self,
        *,
        status: str,
        config_version: int | None,
        correlation_id: str | None,
        requires_restart: bool,
        reported_hash: str | None,
        rejection_reason: str | None,
    ):
        self.status = status
        self.config_version = config_version
        self.correlation_id = correlation_id
        self.requires_restart = requires_restart
        self.reported_hash = reported_hash
        self.rejection_reason = rejection_reason


def _prepare_config_payload(
    config_data: dict[str, Any],
    *,
    config_version: int,
    updated_at: str,
) -> dict[str, Any]:
    prepared = copy.deepcopy(config_data)
    if "version" in prepared and prepared["version"] != config_version:
        raise RemoteConfigError("desired_config.version diverge de config_version")
    prepared["version"] = config_version
    prepared["updatedAt"] = updated_at
    return prepared


def _validate_signature(
    payload: dict[str, Any],
    desired_hash: str,
    device_secret: str,
) -> None:
    if not device_secret:
        raise RemoteConfigError("DEVICE_SECRET ausente para validar config remota")
    signature = _required_str(payload, "signature")
    signature_version = str(payload.get("signature_version") or _SIGNATURE_VERSION)
    if signature_version != _SIGNATURE_VERSION:
        raise RemoteConfigError("signature_version não suportada")
    canonical = _canonical_signature_payload(payload, desired_hash)
    expected = hmac_sha256_base64(device_secret, canonical)
    if not hmac.compare_digest(signature, expected):
        raise RemoteConfigError("assinatura de config inválida")


def _canonical_signature_payload(payload: dict[str, Any], desired_hash: str) -> str:
    return ":".join(
        [
            "v1",
            "CONFIG_DESIRED",
            _required_str(payload, "device_id"),
            str(_required_int(payload, "config_version")),
            _required_str(payload, "correlation_id"),
            _required_str(payload, "issued_at"),
            _required_str(payload, "expires_at"),
            desired_hash,
        ]
    )


def _canonical_report_signature_payload(payload: dict[str, Any]) -> str:
    config_version = payload.get("config_version")
    return ":".join(
        [
            "v1",
            "CONFIG_REPORTED",
            _required_str(payload, "device_id"),
            str(config_version if isinstance(config_version, int) else ""),
            str(payload.get("correlation_id") or ""),
            _required_str(payload, "reported_at"),
            _required_str(payload, "status"),
            str(payload.get("reported_hash") or ""),
        ]
    )


def _validate_hash(config_data: dict[str, Any], desired_hash: str) -> None:
    expected = hash_config(config_data)
    if desired_hash != expected:
        raise RemoteConfigError("desired_hash divergente")


def _validate_remote_config(config_data: dict[str, Any]) -> None:
    missing = sorted(key for key in _REQUIRED_REMOTE_CONFIG_KEYS if key not in config_data)
    required_errors = [
        f"desired_config deve incluir domínio {key}" for key in missing
    ]
    allowlist_errors = _validate_allowlist(config_data)
    schema_errors = validate_config_dict(config_data)
    errors = required_errors + allowlist_errors + schema_errors
    if errors:
        raise RemoteConfigError("; ".join(errors))


def _validate_allowlist(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        allowed = _ALLOWED_KEYS_BY_PATH.get(path)
        for key, child in value.items():
            normalized_key = str(key)
            marker_key = normalized_key.lower().replace("-", "").replace(".", "")
            child_path = path + (normalized_key,)
            if child_path not in _ALLOWED_TOKEN_PATHS and any(
                marker in marker_key for marker in _FORBIDDEN_KEY_MARKERS
            ):
                errors.append(f"{_format_path(path + (normalized_key,))} não é permitido")
            if allowed is not None and normalized_key not in allowed:
                errors.append(f"{_format_path(path + (normalized_key,))} não é permitido")
            errors.extend(_validate_allowlist(child, child_path))
    elif isinstance(value, list):
        list_path = path + ("*",)
        for item in value:
            errors.extend(_validate_allowlist(item, list_path))
    elif path[-1:] == ("rtspUrl",) and isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("rtsp://") and "@" in stripped.split("://", 1)[1].split("/", 1)[0]:
            errors.append(f"{_format_path(path)} não pode conter credenciais em texto plano")
    return errors


def _requires_restart(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for restart_path in _RESTART_PATHS:
        if _has_path(desired, restart_path) and _get_by_path(
            current, restart_path
        ) != _get_by_path(desired, restart_path):
            return True
    return False


def _has_path(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    value: Any = data
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _get_by_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def hash_config(config_data: dict[str, Any]) -> str:
    canonical = json.dumps(
        config_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{_HASH_PREFIX}{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def sign_desired_config_payload(
    *,
    payload: dict[str, Any],
    device_secret: str,
) -> str:
    desired_hash = _required_str(payload, "desired_hash")
    canonical = _canonical_signature_payload(payload, desired_hash)
    return hmac_sha256_base64(device_secret, canonical)


def sign_reported_config_payload(
    *,
    payload: dict[str, Any],
    device_secret: str,
) -> str:
    canonical = _canonical_report_signature_payload(payload)
    return hmac_sha256_base64(device_secret, canonical)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise RemoteConfigError(f"{key} é obrigatório")
    return str(value).strip()


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise RemoteConfigError(f"{key} deve ser inteiro")
    return value


def _safe_int_from_payload(raw_payload: bytes) -> int | None:
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("config_version"), int):
            return payload["config_version"]
    except Exception:
        return None
    return None


def _parse_iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RemoteConfigError(f"timestamp inválido: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _format_path(path: tuple[str, ...]) -> str:
    return ".".join(path).replace(".*.", "[].")


def _sanitize_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    sanitized = str(reason).replace("\n", " ").replace("\r", " ")
    return sanitized[:240]
