from __future__ import annotations

import unittest

from src.services.retry_upload import _sanitize_backend_response


class RetryUploadTests(unittest.TestCase):
    def test_sanitizes_signed_upload_url_from_backend_response(self) -> None:
        response = {
            "success": True,
            "data": {
                "clip_id": "clip-01",
                "upload_url": "https://storage.example.com/signed?signature=fixture",
                "nested": [{"signed_upload_url": "https://storage.example.com/other"}],
            },
        }

        sanitized = _sanitize_backend_response(response)

        self.assertEqual(sanitized["data"]["clip_id"], "clip-01")
        self.assertEqual(sanitized["data"]["upload_url"], "[redacted]")
        self.assertEqual(sanitized["data"]["nested"][0]["signed_upload_url"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
