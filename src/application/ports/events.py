"""Outbound event publication port."""

from collections.abc import Mapping
from typing import Protocol


class EventPublisher(Protocol):
    def publish(self, event_name: str, payload: Mapping[str, object]) -> None: ...
