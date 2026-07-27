from __future__ import annotations

from typing import Protocol


class MessageSigner(Protocol):
    def sign(self, secret: str, message: str) -> str: ...


class EnvEnvelopeCipher(Protocol):
    def seal(
        self,
        secret: str,
        request_id: str,
        device_id: str,
        plaintext: str,
        *,
        issued_at: str | None = None,
    ) -> dict[str, str]: ...

    def open(self, secret: str, envelope: dict[str, str]) -> str: ...
