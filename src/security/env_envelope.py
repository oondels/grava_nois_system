"""
Helpers de envelope criptografado para tráfego seguro de .env via MQTT.

Fluxo:
  1. Derivar chave AES-256 via HKDF-SHA256(device_secret, salt=request_id).
  2. Criptografar conteúdo com AES-256-GCM (iv aleatório de 12 bytes).
  3. Assinar envelope completo com HMAC-SHA256(device_secret, canonical).

Compatível com grava_nois_api/src/utils/envEnvelope.ts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256

_AES_KEY_LENGTH = 32  # 256 bits
_IV_LENGTH = 12  # GCM standard
_AUTH_TAG_LENGTH = 16  # 128-bit tag (GCM appends tag to ciphertext)
_HKDF_INFO = b"grn-env-envelope-v1"
_SIGNATURE_VERSION = "v1"


# ─── Key derivation ─────────────────────────────────────────────────────────


def derive_aes_key(device_secret: str, request_id: str) -> bytes:
    """Derivar chave AES-256 via HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=SHA256(),
        length=_AES_KEY_LENGTH,
        salt=request_id.encode("utf-8"),
        info=_HKDF_INFO,
    )
    return hkdf.derive(device_secret.encode("utf-8"))


# ─── Encrypt / Decrypt ──────────────────────────────────────────────────────


def _encrypt_aes256_gcm(
    key: bytes, plaintext: bytes
) -> tuple[bytes, bytes, bytes]:
    """Retorna (iv, ciphertext, auth_tag)."""
    iv = os.urandom(_IV_LENGTH)
    aesgcm = AESGCM(key)
    # AESGCM.encrypt retorna ciphertext + tag concatenados
    ct_with_tag = aesgcm.encrypt(iv, plaintext, None)
    ciphertext = ct_with_tag[:-_AUTH_TAG_LENGTH]
    auth_tag = ct_with_tag[-_AUTH_TAG_LENGTH:]
    return iv, ciphertext, auth_tag


def _decrypt_aes256_gcm(
    key: bytes, iv: bytes, ciphertext: bytes, auth_tag: bytes
) -> bytes:
    """Descriptografa e verifica autenticidade."""
    aesgcm = AESGCM(key)
    ct_with_tag = ciphertext + auth_tag
    return aesgcm.decrypt(iv, ct_with_tag, None)


# ─── HMAC / Hash ────────────────────────────────────────────────────────────


def _content_hash(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _hmac_sign(secret: str, message: str) -> str:
    digest = hmac_mod.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


# ─── Canonical string ───────────────────────────────────────────────────────


def _build_canonical(envelope: dict[str, Any]) -> str:
    return ":".join(
        [
            _SIGNATURE_VERSION,
            "ENV_ENVELOPE",
            envelope["device_id"],
            envelope["request_id"],
            envelope["issued_at"],
            envelope["content_hash"],
            envelope["iv"],
            envelope["auth_tag"],
        ]
    )


# ─── Public API ──────────────────────────────────────────────────────────────


def seal_env_envelope(
    device_secret: str,
    request_id: str,
    device_id: str,
    plaintext: str,
    issued_at: str | None = None,
) -> dict[str, str]:
    """Criptografa e assina conteúdo de .env em um envelope seguro."""
    from datetime import datetime, timezone

    if issued_at is None:
        issued_at = datetime.now(timezone.utc).isoformat()

    plaintext_bytes = plaintext.encode("utf-8")
    key = derive_aes_key(device_secret, request_id)
    iv, ciphertext, auth_tag = _encrypt_aes256_gcm(key, plaintext_bytes)

    partial: dict[str, str] = {
        "version": _SIGNATURE_VERSION,
        "request_id": request_id,
        "device_id": device_id,
        "issued_at": issued_at,
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "auth_tag": base64.b64encode(auth_tag).decode("ascii"),
        "content_hash": _content_hash(plaintext_bytes),
    }

    canonical = _build_canonical(partial)
    partial["signature"] = _hmac_sign(device_secret, canonical)

    return partial


def open_env_envelope(
    device_secret: str,
    envelope: dict[str, str],
) -> str:
    """Valida assinatura e descriptografa envelope de .env."""
    version = envelope.get("version", "")
    if version != _SIGNATURE_VERSION:
        raise ValueError(f"Versão de envelope não suportada: {version}")

    # Verificar assinatura HMAC
    partial = {k: v for k, v in envelope.items() if k != "signature"}
    canonical = _build_canonical(partial)
    expected_sig = _hmac_sign(device_secret, canonical)

    received_sig = envelope.get("signature", "")
    if not hmac_mod.compare_digest(received_sig, expected_sig):
        raise ValueError("Assinatura de envelope inválida")

    # Descriptografar
    key = derive_aes_key(device_secret, envelope["request_id"])
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["ciphertext"])
    auth_tag = base64.b64decode(envelope["auth_tag"])

    plaintext = _decrypt_aes256_gcm(key, iv, ciphertext, auth_tag)

    # Verificar integridade
    computed_hash = _content_hash(plaintext)
    if computed_hash != envelope["content_hash"]:
        raise ValueError("Hash de conteúdo não corresponde ao esperado")

    return plaintext.decode("utf-8")
