from __future__ import annotations

import unittest

from src.services.retry_upload import _sanitize_backend_response
from src.services.backend_response_sanitizer import redact_url_for_log


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

    def test_redacts_signed_url_for_log(self) -> None:
        safe = redact_url_for_log(
            "https://access:secret@s3.example.com/bucket/file.mp4?X-Amz-Signature=secret"
        )

        self.assertEqual(safe, "https://s3.example.com/bucket/file.mp4?[redacted]")
        self.assertNotIn("secret", safe)
        self.assertNotIn("X-Amz-Signature", safe)


if __name__ == "__main__":
    unittest.main()
