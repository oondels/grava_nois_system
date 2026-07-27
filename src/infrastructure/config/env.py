"""Environment adapters for secrets and device identity."""

import os
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from src.domain.configuration import DeviceIdentity


class EnvSecretsProvider:
    """Expose only explicitly allowed environment variables as secrets."""

    def __init__(
        self,
        names: Iterable[str],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        source = os.environ if environ is None else environ
        self._values = MappingProxyType({name: source[name] for name in names if name in source})

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def snapshot(self) -> Mapping[str, str]:
        return self._values


def device_identity_from_env(
    environ: Mapping[str, str] | None = None,
) -> DeviceIdentity:
    source = os.environ if environ is None else environ

    def first(*names: str) -> str:
        return next(
            (value.strip() for name in names if (value := source.get(name)) and value.strip()),
            "",
        )

    return DeviceIdentity(
        device_id=first("DEVICE_ID", "GN_DEVICE_ID", "GN_MQTT_CLIENT_ID"),
        client_id=first("GN_CLIENT_ID", "CLIENT_ID"),
        venue_id=first("GN_VENUE_ID", "VENUE_ID"),
    )
