from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.config_loader import reset_config_cache
from src.video.processor import add_image_watermark
from src.workers.processing_worker import ProcessingWorker


class VerticalFormatTests(unittest.TestCase):
    """Testes para VERTICAL_FORMAT: crop 9:16 sem scale forçado."""

    def _create_dummy_file(self, path: Path) -> None:
        path.write_bytes(b"dummy")

    def _run_watermark(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            input_video = base / "input.mp4"
            watermark = base / "watermark.png"
            output = base / "output.mp4"
            self._create_dummy_file(input_video)
            self._create_dummy_file(watermark)

            with patch("src.video.processor.ffprobe_metadata", return_value=kwargs.pop("meta")), \
                 patch("src.video.processor.subprocess.run") as mock_run:
                add_image_watermark(
                    input_path=str(input_video),
                    watermark_path=str(watermark),
                    output_path=str(output),
                    **kwargs,
                )
                cmd = mock_run.call_args[0][0]
                return " ".join(cmd)

    def test_vertical_format_applies_crop_filter(self) -> None:
        """vertical_format=True insere crop=ih*9/16:ih no filter_complex."""
        meta = {"codec": "h264", "width": 1920, "height": 1080, "fps": 30.0, "duration_sec": 10.0}
        cmd_str = self._run_watermark(vertical_format=True, meta=meta)
        self.assertIn("crop=ih*9/16:ih:(iw-ih*9/16)/2:0", cmd_str)

    def test_vertical_format_no_forced_scale(self) -> None:
        """vertical_format=True não força scale para 1080x1920 — apenas crop."""
        meta = {"codec": "h264", "width": 1920, "height": 1080, "fps": 30.0, "duration_sec": 10.0}
        cmd_str = self._run_watermark(vertical_format=True, meta=meta)
        self.assertNotIn("scale=1080:1920", cmd_str)
        self.assertNotIn("scale=-2:720", cmd_str)

    def test_vertical_format_false_no_crop(self) -> None:
        """vertical_format=False não insere crop."""
        meta = {"codec": "h264", "width": 1920, "height": 1080, "fps": 30.0, "duration_sec": 10.0}
        cmd_str = self._run_watermark(vertical_format=False, meta=meta)
        self.assertNotIn("crop=ih*9/16", cmd_str)

    def test_no_mobile_format_param(self) -> None:
        """add_image_watermark não aceita mais mobile_format — verificação de assinatura."""
        import inspect
        sig = inspect.signature(add_image_watermark)
        self.assertNotIn("mobile_format", sig.parameters)


class WatermarkAlwaysPresentTests(unittest.TestCase):
    """Watermark deve ser aplicada em ambos os modos (light e HQ)."""

    def setUp(self) -> None:
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()

    def _make_worker(self, base: Path, light_mode: bool) -> ProcessingWorker:
        return ProcessingWorker(
            queue_dir=base / "queue_raw",
            out_wm_dir=base / "highlights_wm",
            failed_dir_highlight=base / "failed_clips",
            watermark_path=Path("/dev/null"),
            scan_interval=0,
            light_mode=light_mode,
            retry_failed=False,
        )

    def _place_mp4_with_sidecar(self, queue: Path, name: str) -> Path:
        queue.mkdir(parents=True, exist_ok=True)
        mp4 = queue / name
        mp4.write_bytes(b"\x00" * 64)
        meta = {
            "type": "highlight_raw",
            "file_name": name,
            "size_bytes": 64,
            "sha256": None,
            "status": "queued",
            "attempts": 0,
            "meta": {"width": 1920, "height": 1080, "fps": 30.0, "duration_sec": 10.0},
        }
        (queue / f"{mp4.stem}.json").write_text(json.dumps(meta))
        return mp4

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.add_image_watermark")
    def test_hq_mode_applies_watermark(self, mock_wm, _ffprobe, mock_api_cls):
        """Modo alta qualidade (light_mode=False) sempre aplica watermark."""
        mock_api_cls.return_value.is_configured.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base, light_mode=False)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_test.mp4")

            with patch.dict(os.environ, {"DEV": "true"}):
                worker._scan_once()

        self.assertTrue(mock_wm.called, "Watermark deve ser aplicada em modo HQ")

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.add_image_watermark")
    def test_light_mode_applies_watermark(self, mock_wm, _ffprobe, mock_api_cls):
        """Modo leve (light_mode=True) também sempre aplica watermark."""
        mock_api_cls.return_value.is_configured.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base, light_mode=True)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_test.mp4")

            with patch.dict(os.environ, {"DEV": "true"}):
                worker._scan_once()

        self.assertTrue(mock_wm.called, "Watermark deve ser aplicada em modo leve")


class WatermarkQualityModeTests(unittest.TestCase):
    """Verifica que CRF e preset corretos são usados por modo."""

    def setUp(self) -> None:
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()

    def _make_worker(self, base: Path, light_mode: bool) -> ProcessingWorker:
        return ProcessingWorker(
            queue_dir=base / "queue_raw",
            out_wm_dir=base / "highlights_wm",
            failed_dir_highlight=base / "failed_clips",
            watermark_path=Path("/dev/null"),
            scan_interval=0,
            light_mode=light_mode,
            retry_failed=False,
        )

    def _place_mp4_with_sidecar(self, queue: Path, name: str) -> Path:
        queue.mkdir(parents=True, exist_ok=True)
        mp4 = queue / name
        mp4.write_bytes(b"\x00" * 64)
        meta = {
            "type": "highlight_raw",
            "file_name": name,
            "size_bytes": 64,
            "sha256": None,
            "status": "queued",
            "attempts": 0,
            "meta": {"width": 1920, "height": 1080, "fps": 30.0, "duration_sec": 10.0},
        }
        (queue / f"{mp4.stem}.json").write_text(json.dumps(meta))
        return mp4

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.add_image_watermark")
    def test_hq_mode_uses_hq_crf_and_preset(self, mock_wm, _ffprobe, mock_api_cls):
        """Modo HQ passa hq_crf e hq_preset para add_image_watermark."""
        mock_api_cls.return_value.is_configured.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base, light_mode=False)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_hq.mp4")

            with patch.dict(
                os.environ,
                {"DEV": "true", "GN_HQ_CRF": "18", "GN_HQ_PRESET": "medium"},
            ):
                worker._scan_once()

        self.assertTrue(mock_wm.called)
        _, kwargs = mock_wm.call_args
        self.assertEqual(kwargs.get("crf"), 18)
        self.assertEqual(kwargs.get("preset"), "medium")

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.add_image_watermark")
    def test_light_mode_uses_lm_crf_and_preset(self, mock_wm, _ffprobe, mock_api_cls):
        """Modo leve passa lm_crf e lm_preset para add_image_watermark."""
        mock_api_cls.return_value.is_configured.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base, light_mode=True)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_lm.mp4")

            with patch.dict(
                os.environ,
                {"DEV": "true", "GN_LM_CRF": "26", "GN_LM_PRESET": "veryfast"},
            ):
                worker._scan_once()

        self.assertTrue(mock_wm.called)
        _, kwargs = mock_wm.call_args
        self.assertEqual(kwargs.get("crf"), 26)
        self.assertEqual(kwargs.get("preset"), "veryfast")


if __name__ == "__main__":
    unittest.main()
