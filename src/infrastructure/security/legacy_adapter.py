from __future__ import annotations

from src.security.env_envelope import open_env_envelope, seal_env_envelope
from src.security.hmac import hmac_sha256_base64


class LegacyHmacSigner:
    def sign(self, secret: str, message: str) -> str:
        return hmac_sha256_base64(secret, message)


class LegacyEnvEnvelopeCipher:
    def seal(
        self,
        secret: str,
        request_id: str,
        device_id: str,
        plaintext: str,
        *,
        issued_at: str | None = None,
    ) -> dict[str, str]:
        return seal_env_envelope(
            secret,
            request_id,
            device_id,
            plaintext,
            issued_at=issued_at,
        )

    def open(self, secret: str, envelope: dict[str, str]) -> str:
        return open_env_envelope(secret, envelope)
