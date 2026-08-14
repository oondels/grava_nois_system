from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.config.settings import load_client_watermark_enabled


class ClientWatermarkConfigTests(unittest.TestCase):
    def test_client_watermark_is_enabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(load_client_watermark_enabled())

    def test_client_watermark_accepts_enabled_values(self) -> None:
        for value in ("1", "true", "yes", "y", "on", "TRUE"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {"GN_CLIENT_WATERMARK_ENABLED": value},
                    clear=True,
                ),
            ):
                self.assertTrue(load_client_watermark_enabled())

    def test_client_watermark_is_disabled_by_false_values(self) -> None:
        for value in ("0", "false", "no", "off", ""):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {"GN_CLIENT_WATERMARK_ENABLED": value},
                    clear=True,
                ),
            ):
                self.assertFalse(load_client_watermark_enabled())


if __name__ == "__main__":
    unittest.main()
