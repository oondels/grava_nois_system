from unittest import TestCase
from unittest.mock import patch

from src.services.api_client import GravaNoisAPIClient


class RentalApiClientTests(TestCase):
    def test_rental_requires_empty_venue(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GN_DEVICE_MODE": "rental",
                "GN_VENUE_ID": "venue-invalid",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "deve ficar vazio"):
                GravaNoisAPIClient(api_base="https://api.example.test")

    def test_fixed_requires_venue(self) -> None:
        with patch.dict(
            "os.environ",
            {"GN_DEVICE_MODE": "fixed", "GN_CLIENT_ID": "client-fixed"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "não configurado"):
                GravaNoisAPIClient(api_base="https://api.example.test")

    def test_rental_accepts_complete_hmac_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GN_DEVICE_MODE": "rental",
                "GN_DEVICE_ID": "rental-01",
                "GN_DEVICE_SECRET": "secret-rental",
            },
            clear=True,
        ):
            client = GravaNoisAPIClient(api_base="https://api.example.test")

        self.assertTrue(client.is_configured())
        self.assertEqual(client.venue_id, None)
        self.assertIsNone(client.client_id)

    def test_extracts_official_clip_registration_envelope(self) -> None:
        clip = GravaNoisAPIClient.extract_clip_registration(
            {
                "success": True,
                "data": {
                    "clip": {
                        "clip_id": "clip-01",
                        "upload_url": "https://storage.example.test/signed",
                    }
                },
            }
        )

        self.assertEqual(clip["clip_id"], "clip-01")

    def test_rejects_registration_without_clip(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "sem objeto clip"):
            GravaNoisAPIClient.extract_clip_registration({"data": {}})
