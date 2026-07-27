from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

MessageHandler = Callable[[str, bytes], None]


class JsonMessagePublisher(Protocol):
    def publish_json(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        retain: bool = False,
    ) -> bool: ...


class MessageSubscriber(Protocol):
    def subscribe(self, topic: str, handler: MessageHandler) -> bool: ...
