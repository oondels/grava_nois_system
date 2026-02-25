"""Helpers de hash e HMAC para assinatura das requests do device."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Union


BytesLike = Union[str, bytes]


def _to_bytes(value: BytesLike) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def sha256_base64(input_data: BytesLike) -> str:
    digest = hashlib.sha256(_to_bytes(input_data)).digest()
    return base64.b64encode(digest).decode("ascii")


def hmac_sha256_base64(secret: str, message: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def make_nonce() -> str:
    return str(uuid.uuid4())


def make_timestamp_sec() -> str:
    return str(int(time.time()))


def sha256Base64(input_data: BytesLike) -> str:
    return sha256_base64(input_data)


def hmacSha256Base64(secret: str, message: str) -> str:
    return hmac_sha256_base64(secret, message)


def makeNonce() -> str:
    return make_nonce()


def makeTimestampSec() -> str:
    return make_timestamp_sec()
