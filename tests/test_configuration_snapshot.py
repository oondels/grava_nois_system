import unittest
from dataclasses import FrozenInstanceError

from src.config.config_loader import CameraConfig, OperationalConfig
from src.infrastructure.config import (
    EnvSecretsProvider,
    LegacyOperationalConfigAdapter,
    device_identity_from_env,
)


class ConfigurationSnapshotTests(unittest.TestCase):
    def test_snapshot_is_deeply_detached_from_legacy_config(self):
        legacy = OperationalConfig(cameras=[CameraConfig(id="cam-1")])

        snapshot = LegacyOperationalConfigAdapter(legacy).load()
        legacy.cameras.append(CameraConfig(id="cam-2"))

        self.assertEqual(("cam-1",), tuple(c.camera_id for c in snapshot.cameras))
        self.assertEqual(40.0, snapshot.capture.buffer_seconds)
        with self.assertRaises(FrozenInstanceError):
            snapshot.capture.buffer_seconds = 99  # type: ignore[misc]

    def test_default_buffer_accounts_for_window_and_safety_margin(self):
        legacy = OperationalConfig()
        legacy.capture.segment_seconds = 4
        legacy.capture.pre_segments = 8
        legacy.capture.post_segments = 4

        snapshot = LegacyOperationalConfigAdapter(legacy).load()

        self.assertEqual(56.0, snapshot.capture.buffer_seconds)

    def test_explicit_buffer_is_preserved(self):
        snapshot = LegacyOperationalConfigAdapter(OperationalConfig(), buffer_seconds=75).load()
        self.assertEqual(75.0, snapshot.capture.buffer_seconds)

    def test_environment_adapters_use_precedence_and_allowlist(self):
        environ = {
            "DEVICE_ID": " device-primary ",
            "GN_DEVICE_ID": "device-fallback",
            "GN_CLIENT_ID": "client-1",
            "VENUE_ID": "venue-1",
            "SECRET": "value",
            "NOT_ALLOWED": "hidden",
        }

        identity = device_identity_from_env(environ)
        secrets = EnvSecretsProvider(["SECRET"], environ)

        self.assertEqual("device-primary", identity.device_id)
        self.assertEqual("client-1", identity.client_id)
        self.assertEqual("venue-1", identity.venue_id)
        self.assertEqual("fixed", identity.device_mode)
        self.assertEqual({"SECRET": "value"}, dict(secrets.snapshot()))
        self.assertIsNone(secrets.get("NOT_ALLOWED"))

    def test_rental_identity_requires_null_venue(self):
        identity = device_identity_from_env(
            {
                "GN_DEVICE_MODE": "rental",
                "DEVICE_ID": "rental-01",
                "GN_CLIENT_ID": "",
                "GN_VENUE_ID": "",
            }
        )

        self.assertEqual("rental", identity.device_mode)
        self.assertTrue(identity.is_rental)
        self.assertIsNone(identity.client_id)
        self.assertIsNone(identity.venue_id)

    def test_rental_identity_rejects_fixed_venue(self):
        with self.assertRaisesRegex(Exception, "must not have venue"):
            device_identity_from_env(
                {
                    "GN_DEVICE_MODE": "rental",
                    "DEVICE_ID": "rental-01",
                    "GN_CLIENT_ID": "",
                    "GN_VENUE_ID": "venue-1",
                }
            )


if __name__ == "__main__":
    unittest.main()
