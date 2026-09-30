from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.services.backend_response_sanitizer import redact_url_for_log
from src.services.retry_upload import _sanitize_backend_response, retry_failed_uploads


class RetryUploadTests(unittest.TestCase):
    def test_confirmed_old_upload_is_cleaned_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            sidecar = root / "clip.json"
            sidecar.write_text(
                json.dumps(
                    {
                        "status": "uploaded",
                        "remote_registration": {"status": "registered"},
                        "remote_upload": {"status": "uploaded"},
                        "remote_finalize": {
                            "status": "ok",
                            "finalized_at": "2026-09-29T10:00:00+00:00",
                            "response": {
                                "data": {"clip": {"clip_id": "clip-id", "status": "uploaded"}}
                            },
                        },
                    }
                )
            )
            client = Mock()
            client.is_configured.return_value = False

            result = retry_failed_uploads(root, api_client=client)

            self.assertEqual(result, {"processed": 1, "uploaded": 1, "failed": 0})
            self.assertFalse(video.exists())
            self.assertFalse(sidecar.exists())
            client.register_clip_metadados.assert_not_called()

    def test_ambiguous_previous_upload_is_preserved_without_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            sidecar = root / "clip.json"
            sidecar.write_text(
                json.dumps({"status": "retry_uploading", "remote_upload": {"status": "uploaded"}})
            )
            client = Mock()
            client.is_configured.return_value = True

            result = retry_failed_uploads(root, api_client=client)

            self.assertEqual(result, {"processed": 1, "uploaded": 0, "failed": 1})
            self.assertTrue(video.exists())
            client.register_clip_metadados.assert_not_called()

    def test_missing_sidecar_is_preserved_without_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            client = Mock()
            client.is_configured.return_value = True

            result = retry_failed_uploads(root, api_client=client)

            self.assertEqual(result, {"processed": 1, "uploaded": 0, "failed": 1})
            self.assertTrue(video.exists())
            self.assertFalse((root / "clip.json").exists())
            client.register_clip_metadados.assert_not_called()

    def test_successful_finalize_removes_video_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            (root / "clip.json").write_text(json.dumps({"status": "upload_pending", "attempts": 0}))
            client = Mock(venue_id="venue")
            client.is_configured.return_value = True
            client.register_clip_metadados.return_value = {"success": True}
            client.extract_clip_registration.return_value = {
                "clip_id": "clip-id",
                "upload_url": "https://example.test/upload",
            }
            client.upload_file_to_signed_url.return_value = (200, "ok", {})
            client.finalize_clip_uploaded.return_value = {
                "data": {"clip": {"clip_id": "clip-id", "status": "uploaded"}}
            }

            with (
                patch(
                    "src.services.retry_upload.ffprobe_metadata", return_value={"duration_sec": 1.0}
                ),
                patch("src.services.retry_upload._sha256_file", return_value="a" * 64),
            ):
                result = retry_failed_uploads(root, api_client=client)

            self.assertEqual(result, {"processed": 1, "uploaded": 1, "failed": 0})
            self.assertFalse(video.exists())
            self.assertFalse((root / "clip.json").exists())
            client.finalize_clip_uploaded.assert_called_once()

    def test_dry_run_finalize_preserves_uploaded_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            (root / "clip.json").write_text(json.dumps({"status": "upload_pending", "attempts": 0}))
            client = Mock(venue_id="venue")
            client.is_configured.return_value = True
            client.register_clip_metadados.return_value = {"success": True}
            client.extract_clip_registration.return_value = {
                "clip_id": "clip-id",
                "upload_url": "https://example.test/upload",
            }
            client.upload_file_to_signed_url.return_value = (200, "ok", {})
            client.finalize_clip_uploaded.return_value = {"dry_run": True}

            with (
                patch(
                    "src.services.retry_upload.ffprobe_metadata", return_value={"duration_sec": 1.0}
                ),
                patch("src.services.retry_upload._sha256_file", return_value="a" * 64),
            ):
                result = retry_failed_uploads(root, api_client=client)

            self.assertEqual(result, {"processed": 1, "uploaded": 0, "failed": 1})
            self.assertTrue(video.exists())
            self.assertEqual(
                json.loads((root / "clip.json").read_text())["remote_finalize"]["status"],
                "uncertain",
            )

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
