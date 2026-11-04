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

    def test_rental_registration_preserves_camera_and_omits_venue(self):
        with patch.dict("os.environ", {"GN_DEVICE_MODE": "rental", "GN_DEVICE_ID": "rental-test", "GN_DEVICE_SECRET": "test-secret"}, clear=True):
            client = GravaNoisAPIClient(api_base="https://api.example.test")
        with patch.object(client, "_http_post_json", return_value={}) as post:
            client.register_clip_metadados({"camera_id": "cam01", "venue_id": None, "sha256": "a" * 64, "meta": {}})
        self.assertEqual(post.call_args.args[0], "https://api.example.test/api/videos/rental/metadata")
        self.assertEqual(post.call_args.args[1]["camera_id"], "cam01")
        self.assertNotIn("venue_id", post.call_args.args[1])
