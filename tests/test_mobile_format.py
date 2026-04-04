from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.video.processor import add_image_watermark


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


if __name__ == "__main__":
    unittest.main()
