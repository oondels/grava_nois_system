"""Authenticated v2 administrative control (shared wire contract with the API)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from src.security.hmac import hmac_sha256_base64


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sign_control(secret: str, payload: dict[str, Any]) -> dict[str, Any]:
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    unsigned["signature_version"] = "hmac-sha256-v2"
    return {
        **unsigned,
        "signature": hmac_sha256_base64(secret, "v2:ENV_CONTROL:" + canonical_json(unsigned)),
    }


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid_timestamp")
    return parsed


def validate_control(
    secret: str,
    payload: dict[str, Any],
    device_id: str,
    expected_type: str,
    *,
    now: datetime | None = None,
) -> None:
    if not secret or not isinstance(payload, dict):
        raise ValueError("invalid_control")
    if payload.get("signature_version") != "hmac-sha256-v2" or payload.get("type") != expected_type:
        raise ValueError("unsupported_control")
    if payload.get("device_id") != device_id:
        raise ValueError("device_mismatch")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
        raise ValueError("invalid_request_id")
    signature = payload.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, sign_control(secret, payload)["signature"]
    ):
        raise ValueError("invalid_signature")
    issued, expires = timestamp(payload.get("issued_at")), timestamp(payload.get("expires_at"))
    now = now or datetime.now(UTC)
    if not timedelta(0) < expires - issued <= timedelta(seconds=120):
        raise ValueError("invalid_lifetime")
    if expires <= now or issued > now + timedelta(seconds=30):
        raise ValueError("expired_or_future_control")


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
