"""Sanitizacao de respostas do backend antes de logar ou persistir."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


SENSITIVE_RESPONSE_KEYS = {"upload_url", "signed_upload_url"}


def sanitize_backend_response(value):
    """Remove URLs assinadas e outros campos sensiveis de payloads aninhados."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_RESPONSE_KEYS:
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = sanitize_backend_response(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_backend_response(item) for item in value]
    return value


def redact_url_for_log(value: str) -> str:
    """Preserva destino basico da URL e remove query/credenciais de log."""
    try:
        parts = urlsplit(value)
    except Exception:
        return "[redacted-url]"

    if not parts.scheme or not parts.netloc:
        return "[redacted-url]"

    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    query = "[redacted]" if parts.query else ""
    return urlunsplit((parts.scheme, host, path, query, ""))
