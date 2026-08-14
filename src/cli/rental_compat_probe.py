"""Report whether this image implements tenantless rental identity.

This module is intentionally side-effect free: it does not load camera settings,
connect to MQTT/API, or start the capture pipeline.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from src.application.device.models import DeviceIdentity as ApplicationDeviceIdentity
from src.domain.configuration import DeviceIdentity as DomainDeviceIdentity

PROBE_SCHEMA_VERSION = 1
RENTAL_IDENTITY_CONTRACT = "tenantless-v1"


def build_probe_result(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    source = os.environ if environ is None else environ
    agent_version = (source.get("GN_AGENT_VERSION") or "local-dev").strip() or "local-dev"

    domain_identity = DomainDeviceIdentity(
        device_id="rental-compat-probe",
        client_id=None,
        venue_id=None,
        device_mode="rental",
    )
    application_identity = ApplicationDeviceIdentity(
        device_id="rental-compat-probe",
        client_id=None,
        venue_id=None,
        agent_version=agent_version,
        boot_id="rental-compat-probe",
        device_mode="rental",
    )

    compatible = (
        domain_identity.is_rental
        and domain_identity.client_id is None
        and domain_identity.venue_id is None
        and application_identity.client_id is None
        and application_identity.venue_id is None
    )
    return {
        "compatible": compatible,
        "probe_schema_version": PROBE_SCHEMA_VERSION,
        "rental_identity_contract": RENTAL_IDENTITY_CONTRACT,
        "agent_version": agent_version,
    }


def main() -> int:
    result = build_probe_result()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["compatible"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
