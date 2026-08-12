from unittest import TestCase
from unittest.mock import patch

from src.services.api_client import GravaNoisAPIClient


class RentalApiClientTests(TestCase):
    def test_rental_requires_empty_venue(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GN_DEVICE_MODE": "rental",
                "GN_CLIENT_ID": "client-rental",
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
