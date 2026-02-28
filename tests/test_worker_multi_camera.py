"""Tests for ProcessingWorker in multi-camera setup.

Validates that workers with separate queue_dirs operate independently
and do not cross-contaminate each other's artefacts.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.workers.processing_worker import ProcessingWorker


def _make_worker(queue_dir: Path, failed_dir: Path) -> ProcessingWorker:
    """Create a ProcessingWorker in light_mode with no retry loop."""
    return ProcessingWorker(
        queue_dir=queue_dir,
        out_wm_dir=queue_dir / "_wm_unused",
        failed_dir_highlight=failed_dir,
        watermark_path=Path("/dev/null"),
        scan_interval=0,
        light_mode=True,
        retry_failed=False,
    )


def _place_mp4(queue_dir: Path, name: str) -> Path:
    """Create a minimal fake mp4 in queue_dir."""
    p = queue_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 64)
    return p


@patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 5.0})
@patch("src.workers.processing_worker.GravaNoisAPIClient")
class WorkerMultiCameraTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_worker_processes_file_in_own_queue(self, mock_api_cls, _ffprobe):
        """Worker scans its own queue_dir and processes the mp4 it finds."""
        mock_api_cls.return_value.is_configured.return_value = False

        queue = self.base / "queue_raw" / "cam01"
        failed = self.base / "failed_clips" / "cam01"
        queue.mkdir(parents=True, exist_ok=True)

        mp4 = _place_mp4(queue, "highlight_cam01_test.mp4")
        worker = _make_worker(queue, failed)

        with patch.dict(os.environ, {"DEV": ""}):
            worker._scan_once()

        # File must have been consumed: either moved to upload_failed or removed
        upload_failed = failed / "upload_failed" / mp4.name
        self.assertTrue(
            upload_failed.exists() or not mp4.exists(),
            "Worker must process its own file (move to upload_failed or delete)",
        )

    def test_worker_ignores_other_camera_queue(self, mock_api_cls, _ffprobe):
        """Worker does NOT touch files belonging to a different camera's queue_dir."""
        mock_api_cls.return_value.is_configured.return_value = False

        queue_cam01 = self.base / "queue_raw" / "cam01"
        queue_cam02 = self.base / "queue_raw" / "cam02"
        failed_cam01 = self.base / "failed_clips" / "cam01"
        queue_cam01.mkdir(parents=True, exist_ok=True)
        queue_cam02.mkdir(parents=True, exist_ok=True)

        mp4_cam02 = _place_mp4(queue_cam02, "highlight_cam02_test.mp4")

        worker_cam01 = _make_worker(queue_cam01, failed_cam01)
        worker_cam01._scan_once()

        # cam02 file must be completely untouched
        self.assertTrue(mp4_cam02.exists(), "cam02 mp4 must not be moved by cam01 worker")
        sidecar_cam02 = queue_cam02 / "highlight_cam02_test.json"
        self.assertFalse(sidecar_cam02.exists(), "cam01 worker must not create cam02 sidecar")

    def test_two_workers_process_independently(self, mock_api_cls, _ffprobe):
        """Two workers each process their own file without cross-contamination."""
        mock_api_cls.return_value.is_configured.return_value = False

        queue_cam01 = self.base / "queue_raw" / "cam01"
        queue_cam02 = self.base / "queue_raw" / "cam02"
        failed_cam01 = self.base / "failed_clips" / "cam01"
        failed_cam02 = self.base / "failed_clips" / "cam02"
        queue_cam01.mkdir(parents=True, exist_ok=True)
        queue_cam02.mkdir(parents=True, exist_ok=True)

        mp4_cam01 = _place_mp4(queue_cam01, "highlight_cam01_test.mp4")
        mp4_cam02 = _place_mp4(queue_cam02, "highlight_cam02_test.mp4")

        worker_cam01 = _make_worker(queue_cam01, failed_cam01)
        worker_cam02 = _make_worker(queue_cam02, failed_cam02)

        with patch.dict(os.environ, {"DEV": ""}):
            worker_cam01._scan_once()
            worker_cam02._scan_once()

        uf_cam01 = failed_cam01 / "upload_failed" / mp4_cam01.name
        uf_cam02 = failed_cam02 / "upload_failed" / mp4_cam02.name

        self.assertTrue(
            uf_cam01.exists() or not mp4_cam01.exists(),
            "cam01 file must be processed",
        )
        self.assertTrue(
            uf_cam02.exists() or not mp4_cam02.exists(),
            "cam02 file must be processed",
        )

    def test_dev_mode_preserves_file_after_processing(self, mock_api_cls, _ffprobe):
        """In DEV mode, the mp4 and sidecar remain locally preserved."""
        mock_api_cls.return_value.is_configured.return_value = False

        queue = self.base / "queue_raw" / "cam01"
        failed = self.base / "failed_clips" / "cam01"
        queue.mkdir(parents=True, exist_ok=True)

        mp4 = _place_mp4(queue, "highlight_cam01_dev.mp4")
        worker = _make_worker(queue, failed)

        with patch.dict(os.environ, {"DEV": "true"}):
            worker._scan_once()

        self.assertTrue(mp4.exists(), "DEV mode: mp4 must remain in queue")
        sidecar = queue / "highlight_cam01_dev.json"
        self.assertTrue(sidecar.exists(), "DEV mode: sidecar must remain in queue")
        meta = json.loads(sidecar.read_text())
        self.assertEqual(meta.get("status"), "dev_local_preserved")

    def test_no_api_moves_file_to_upload_failed(self, mock_api_cls, _ffprobe):
        """Without API configured, processed file is moved to upload_failed/."""
        mock_api_cls.return_value.is_configured.return_value = False

        queue = self.base / "queue_raw" / "cam02"
        failed = self.base / "failed_clips" / "cam02"
        queue.mkdir(parents=True, exist_ok=True)

        mp4 = _place_mp4(queue, "highlight_cam02_noapitest.mp4")
        worker = _make_worker(queue, failed)

        with patch.dict(os.environ, {"DEV": "", "API_BASE_URL": ""}):
            worker._scan_once()

        upload_failed_path = failed / "upload_failed" / mp4.name
        self.assertTrue(upload_failed_path.exists(), "File must land in upload_failed/ when no API")
        self.assertFalse(mp4.exists(), "Original mp4 must be removed from queue")

    def test_upload_failed_sidecar_has_camera_context(self, mock_api_cls, _ffprobe):
        """Sidecar moved to upload_failed must retain file_name with camera_id."""
        mock_api_cls.return_value.is_configured.return_value = False

        queue = self.base / "queue_raw" / "cam03"
        failed = self.base / "failed_clips" / "cam03"
        queue.mkdir(parents=True, exist_ok=True)

        mp4 = _place_mp4(queue, "highlight_cam03_sidecar.mp4")
        worker = _make_worker(queue, failed)

        with patch.dict(os.environ, {"DEV": "", "API_BASE_URL": ""}):
            worker._scan_once()

        sidecar_path = failed / "upload_failed" / "highlight_cam03_sidecar.json"
        self.assertTrue(sidecar_path.exists(), "Sidecar must be moved alongside mp4")
        meta = json.loads(sidecar_path.read_text())
        self.assertIn("cam03", meta["file_name"], "Sidecar file_name must include camera_id")


if __name__ == "__main__":
    unittest.main()
