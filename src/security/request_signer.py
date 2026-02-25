"""Assinatura HMAC de requests para endpoints sensiveis do backend."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

from src.security.hmac import (
    hmac_sha256_base64,
    make_nonce,
    make_timestamp_sec,
    sha256_base64,
)


_CLIENT_PATH_RE = re.compile(r"^/api/videos/metadados/client/([^/]+)(?:/|$)")


@dataclass(frozen=True)
class SignedRequest:
    headers: Dict[str, str]
    canonical_string: str
    body_sha256: str
    timestamp: str
    nonce: str
    signature: str
    path: str
    method: str


def truncate_signature(signature: str, keep: int = 8) -> str:
    if len(signature) <= keep:
        return signature
    return f"{signature[:keep]}..."


def _derive_client_id_from_path(path: str) -> Optional[str]:
    match = _CLIENT_PATH_RE.match(path)
    if not match:
        return None
    return match.group(1)


def sign_request(
    *,
    method: str,
    path: str,
    body_string: str,
    device_id: str,
    device_secret: str,
    client_id: Optional[str] = None,
    content_type: str = "application/json",
    timestamp: Optional[str] = None,
    nonce: Optional[str] = None,
) -> SignedRequest:
    if not device_id:
        raise ValueError("device_id vazio")
    if not device_secret:
        raise ValueError("device_secret vazio")
    if not path.startswith("/"):
        raise ValueError("path deve comecar com '/'")

    resolved_client_id = client_id or _derive_client_id_from_path(path)
    if not resolved_client_id:
        raise ValueError("nao foi possivel resolver client_id para assinatura")

    used_method = method.upper()
    used_timestamp = timestamp or make_timestamp_sec()
    used_nonce = nonce or make_nonce()
    used_body = body_string if body_string is not None else ""

    body_sha256 = sha256_base64(used_body)
    canonical = (
        f"v1:{used_method}:{path}:{used_timestamp}:{used_nonce}:{body_sha256}"
    )
    signature = hmac_sha256_base64(device_secret, canonical)

    headers = {
        "Content-Type": content_type,
        "X-Device-Id": device_id,
        "X-Client-Id": resolved_client_id,
        "X-Timestamp": used_timestamp,
        "X-Nonce": used_nonce,
        "X-Body-SHA256": body_sha256,
        "X-Signature": signature,
    }

    return SignedRequest(
        headers=headers,
        canonical_string=canonical,
        body_sha256=body_sha256,
        timestamp=used_timestamp,
        nonce=used_nonce,
        signature=signature,
        path=path,
        method=used_method,
    )


def signRequest(**kwargs) -> SignedRequest:
    return sign_request(**kwargs)
