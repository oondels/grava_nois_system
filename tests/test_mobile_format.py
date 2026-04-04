from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.video.processor import add_image_watermark
from src.workers.processing_worker import ProcessingWorker


class MobileFormatTests(unittest.TestCase):
    """Testes para flag MOBILE_FORMAT (redimensionamento para 720p)."""

    def _create_dummy_file(self, path: Path) -> None:
        """Cria arquivo dummy com conteúdo mínimo."""
        path.write_bytes(b"dummy")

    def test_mobile_format_default_true(self) -> None:
        """MOBILE_FORMAT padrão é True (720p máx)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            input_video = base / "input.mp4"
            watermark = base / "watermark.png"
            output = base / "output.mp4"

            self._create_dummy_file(input_video)
            self._create_dummy_file(watermark)

            # Mock ffprobe para retornar vídeo 1080p
            mock_meta = {
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
                "duration_sec": 10.0,
            }

            with patch("src.video.processor.ffprobe_metadata", return_value=mock_meta), \
                 patch("src.video.processor.subprocess.run") as mock_run:

                # Chama sem especificar mobile_format (deve usar padrão True)
                add_image_watermark(
                    input_path=str(input_video),
                    watermark_path=str(watermark),
                    output_path=str(output),
                )

                # Verifica que subprocess.run foi chamado
                self.assertEqual(mock_run.call_count, 1)
                cmd = mock_run.call_args[0][0]

                # Deve conter scale filter para 720p
                cmd_str = " ".join(cmd)
                self.assertIn("scale", cmd_str)
                self.assertIn("720", cmd_str)

    def test_mobile_format_explicit_true(self) -> None:
        """MOBILE_FORMAT=True redimensiona para 720p quando original > 720p."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            input_video = base / "input.mp4"
            watermark = base / "watermark.png"
            output = base / "output.mp4"

            self._create_dummy_file(input_video)
            self._create_dummy_file(watermark)

            mock_meta = {
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
                "duration_sec": 10.0,
            }

            with patch("src.video.processor.ffprobe_metadata", return_value=mock_meta), \
                 patch("src.video.processor.subprocess.run") as mock_run:

                add_image_watermark(
                    input_path=str(input_video),
                    watermark_path=str(watermark),
                    output_path=str(output),
                    mobile_format=True,
                )

                cmd = mock_run.call_args[0][0]
                cmd_str = " ".join(cmd)

                # Deve conter scale para 720p
                self.assertIn("scale=-2:720", cmd_str)

    def test_mobile_format_false_no_scale(self) -> None:
        """MOBILE_FORMAT=False não redimensiona (mantém resolução original)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            input_video = base / "input.mp4"
            watermark = base / "watermark.png"
            output = base / "output.mp4"

            self._create_dummy_file(input_video)
            self._create_dummy_file(watermark)

            mock_meta = {
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
                "duration_sec": 10.0,
            }

            with patch("src.video.processor.ffprobe_metadata", return_value=mock_meta), \
                 patch("src.video.processor.subprocess.run") as mock_run:

                add_image_watermark(
                    input_path=str(input_video),
                    watermark_path=str(watermark),
                    output_path=str(output),
                    mobile_format=False,
                )

                cmd = mock_run.call_args[0][0]
                cmd_str = " ".join(cmd)

                # NÃO deve conter scale filter de vídeo (só do watermark)
                # A escala do watermark é independent
                self.assertNotIn("scale=-2:720", cmd_str)

    def test_mobile_format_no_scale_if_already_720p(self) -> None:
        """MOBILE_FORMAT=True não redimensiona se vídeo já ≤ 720p."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            input_video = base / "input.mp4"
            watermark = base / "watermark.png"
            output = base / "output.mp4"

            self._create_dummy_file(input_video)
            self._create_dummy_file(watermark)

            # Vídeo com 720p (height=720)
            mock_meta = {
                "codec": "h264",
                "width": 1280,
                "height": 720,
                "fps": 30.0,
                "duration_sec": 10.0,
            }

            with patch("src.video.processor.ffprobe_metadata", return_value=mock_meta), \
                 patch("src.video.processor.subprocess.run") as mock_run:

                add_image_watermark(
                    input_path=str(input_video),
                    watermark_path=str(watermark),
                    output_path=str(output),
                    mobile_format=True,
                )

                cmd = mock_run.call_args[0][0]
                cmd_str = " ".join(cmd)

                # NÃO deve conter scale filter (vídeo já é 720p)
                self.assertNotIn("scale=-2:720", cmd_str)


class LightModeMobileFormatTests(unittest.TestCase):
    """Testa que MOBILE_FORMAT é aplicado em modo leve (GN_LIGHT_MODE=1)."""

    def _make_worker(self, base: Path) -> ProcessingWorker:
        return ProcessingWorker(
            queue_dir=base / "queue_raw",
            out_wm_dir=base / "highlights_wm",
            failed_dir_highlight=base / "failed_clips",
            watermark_path=Path("/dev/null"),
            scan_interval=0,
            light_mode=True,
            retry_failed=False,
        )

    def _place_mp4_with_sidecar(self, queue: Path, name: str, height: int = 1080) -> Path:
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
            "meta": {"width": 1920, "height": height, "fps": 30.0, "duration_sec": 10.0},
        }
        (queue / f"{mp4.stem}.json").write_text(json.dumps(meta))
        return mp4

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.subprocess.run")
    def test_light_mode_applies_mobile_format_when_enabled(self, mock_run, _ffprobe, mock_api_cls):
        """Em modo leve + MOBILE_FORMAT=1 + vídeo 1080p: ffmpeg scale é chamado."""
        mock_api_cls.return_value.is_configured.return_value = False
        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_test.mp4", height=1080)

            with patch.dict(os.environ, {"DEV": "true", "MOBILE_FORMAT": "1"}):
                worker._scan_once()

        # subprocess.run deve ter sido chamado com scale=-2:720
        self.assertTrue(mock_run.called, "ffmpeg deve ser chamado para redimensionar")
        cmd = mock_run.call_args[0][0]
        self.assertIn("-vf", cmd)
        self.assertIn("scale=-2:720", cmd)

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.subprocess.run")
    def test_light_mode_skips_mobile_format_when_disabled(self, mock_run, _ffprobe, mock_api_cls):
        """Em modo leve + MOBILE_FORMAT=0: ffmpeg NÃO é chamado para scale."""
        mock_api_cls.return_value.is_configured.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_test.mp4", height=1080)

            with patch.dict(os.environ, {"DEV": "true", "MOBILE_FORMAT": "0"}):
                worker._scan_once()

        # ffmpeg NÃO deve ter sido chamado (sem scale)
        self.assertFalse(mock_run.called, "ffmpeg não deve ser chamado quando MOBILE_FORMAT=0")

    @patch("src.workers.processing_worker.GravaNoisAPIClient")
    @patch("src.workers.processing_worker.ffprobe_metadata", return_value={"duration_sec": 10.0})
    @patch("src.workers.processing_worker.subprocess.run")
    def test_light_mode_skips_mobile_format_if_already_720p(self, mock_run, _ffprobe, mock_api_cls):
        """Em modo leve + MOBILE_FORMAT=1 + vídeo 720p: NÃO redimensiona."""
        mock_api_cls.return_value.is_configured.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = self._make_worker(base)
            mp4 = self._place_mp4_with_sidecar(worker.queue_dir, "highlight_cam01_test.mp4", height=720)

            with patch.dict(os.environ, {"DEV": "true", "MOBILE_FORMAT": "1"}):
                worker._scan_once()

        # ffmpeg NÃO deve ter sido chamado (vídeo já é 720p)
        self.assertFalse(mock_run.called, "ffmpeg não deve ser chamado quando vídeo já é 720p")


if __name__ == "__main__":
    unittest.main()
