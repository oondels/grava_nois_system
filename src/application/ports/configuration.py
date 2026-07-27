"""Configuration and secret source ports."""

from collections.abc import Mapping
from typing import Protocol

from src.domain.configuration import OperationalConfigSnapshot


class OperationalConfigRepository(Protocol):
    def load(self) -> OperationalConfigSnapshot: ...


class SecretsProvider(Protocol):
    def get(self, name: str) -> str | None: ...

    def snapshot(self) -> Mapping[str, str]: ...
