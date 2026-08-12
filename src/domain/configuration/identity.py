"""Immutable device identity, kept separate from operational configuration."""

from dataclasses import dataclass

from src.domain.exceptions import InvariantViolation


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    client_id: str | None
    venue_id: str | None
    device_mode: str = "fixed"

    def __post_init__(self) -> None:
        if self.device_mode not in {"fixed", "rental"}:
            raise InvariantViolation("device mode must be fixed or rental")
        if self.device_mode == "fixed" and not self.venue_id:
            raise InvariantViolation("fixed device requires venue id")
        if self.device_mode == "fixed" and not self.client_id:
            raise InvariantViolation("fixed device requires client id")
        if self.device_mode == "rental" and self.venue_id is not None:
            raise InvariantViolation("rental device must not have venue id")
        if self.device_mode == "rental" and self.client_id is not None:
            raise InvariantViolation("rental device must not have client id")

    @property
    def is_identified(self) -> bool:
        return bool(self.device_id)

    @property
    def is_rental(self) -> bool:
        return self.device_mode == "rental"
