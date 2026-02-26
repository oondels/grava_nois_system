"""
Classificacao de erros da API para decidir acao no device.

Foco: falhas de autenticacao/HMAC e client mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


_DELETE_RECORD_MESSAGES = {
    "missing_headers",
    "invalid_timestamp",
    "invalid_nonce",
    "invalid_body_hash",
    "invalid_signature_format",
    "device_not_found",
    "device_revoked",
    "client_mismatch",
    "missing_raw_body",
    "integrity_failed",
    "signature_mismatch",
    "device_not_authenticated",
}

_DELETE_RECORD_MESSAGE_SNIPPETS = {
    "forbidden - video does not belong to device client",
}


@dataclass(frozen=True)
class APIErrorInfo:
    status_code: Optional[int]
    message: str
    error_code: str
    request_id: str

    @property
    def message_normalized(self) -> str:
        return self.message.strip().lower()

    @property
    def should_delete_local_record(self) -> bool:
        message = self.message_normalized
        if message in _DELETE_RECORD_MESSAGES:
            return True
        return any(snippet in message for snippet in _DELETE_RECORD_MESSAGE_SNIPPETS)

    def short_label(self) -> str:
        status = self.status_code if self.status_code is not None else "?"
        return (
            f"status={status} message={self.message or '-'} "
            f"code={self.error_code or '-'} request_id={self.request_id or '-'}"
        )


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_api_error_from_response(response: Any) -> Optional[APIErrorInfo]:
    if response is None:
        return None

    status_code = getattr(response, "status_code", None)
    payload: Dict[str, Any] = {}
    try:
        raw_payload = response.json()
        if isinstance(raw_payload, dict):
            payload = raw_payload
    except Exception:
        payload = {}

    message = _coerce_text(payload.get("message") or payload.get("detail"))
    error_code = ""
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        error_code = _coerce_text(error_obj.get("code"))
        if not message:
            message = _coerce_text(error_obj.get("message"))

    request_id = _coerce_text(payload.get("requestId") or payload.get("request_id"))

    if not message:
        message = _coerce_text(getattr(response, "text", ""))

    if not message and not error_code and status_code is None:
        return None

    return APIErrorInfo(
        status_code=status_code,
        message=message,
        error_code=error_code,
        request_id=request_id,
    )


def extract_api_error_from_exception(exc: BaseException) -> Optional[APIErrorInfo]:
    current: Optional[BaseException] = exc
    while current is not None:
        if isinstance(current, requests.exceptions.HTTPError):
            return parse_api_error_from_response(getattr(current, "response", None))
        current = getattr(current, "__cause__", None)
    return None
