from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.settings import CaptureConfig
from src.video.processor import WatermarkSpec, build_highlight


def _make_cfg(base: Path) -> CaptureConfig:
    return CaptureConfig(
        camera_id="cam01",
        buffer_dir=base / "buffer",
        clips_dir=base / "clips",
        queue_dir=base / "queue",
        failed_dir_highlight=base / "failed",
        seg_time=1,
        pre_seconds=5,
        post_seconds=3,
    )


def _make_segments(cfg: CaptureConfig, total: int = 10) -> list[str]:
    segs: list[str] = []
    for i in range(total):
        seg = cfg.buffer_dir / f"buffer{i:06d}.ts"
        seg.parent.mkdir(parents=True, exist_ok=True)
        seg.write_bytes(b"x" * 64)
        segs.append(str(seg))
    return segs


class BuildHighlightWatermarkCommandTests(unittest.TestCase):
    def test_build_highlight_with_watermark_uses_single_final_encode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = _make_cfg(base)
            segbuf = MagicMock()
            segbuf.snapshot_last.return_value = _make_segments(cfg)

            wm_dir = base / "highlights_wm"
            wm_dir.mkdir(parents=True, exist_ok=True)
            wm_png = base / "files" / "replay_grava_nois.png"
            wm_png.parent.mkdir(parents=True, exist_ok=True)
            wm_png.write_bytes(b"png")

            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"out")
                return MagicMock()

            with patch("src.video.processor.subprocess.run", side_effect=fake_run), patch(
                "src.video.processor.ffprobe_metadata", return_value={"width": 1280}
            ), patch("src.video.processor.time.sleep"), patch(
                "src.video.processor.time.time", return_value=1700000000.0
            ):
                out = build_highlight(
                    cfg,
                    segbuf,
                    watermark=WatermarkSpec(
                        path=str(wm_png), margin_px=24, opacity=0.8, rel_width=0.11
                    ),
                    output_dir=wm_dir,
                )

            self.assertIsNotNone(out)
            self.assertTrue(out.exists())
            self.assertEqual(out.parent, wm_dir)
            self.assertEqual(len(calls), 2, "Build deve executar 2 comandos ffmpeg")

            final_cmd = calls[1]
            inputs = [final_cmd[i + 1] for i, token in enumerate(final_cmd) if token == "-i"]
            self.assertEqual(len(inputs), 2, "Modo watermarked deve usar 2 inputs")
            self.assertIn(str(wm_png), inputs)
            self.assertIn("-filter_complex", final_cmd)
            filt = final_cmd[final_cmd.index("-filter_complex") + 1]
            self.assertIn("overlay=", filt)
            self.assertIn("colorchannelmixer=aa=0.800", filt)
            self.assertIn("-loop", final_cmd)

    def test_build_highlight_without_watermark_keeps_original_remux(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = _make_cfg(base)
            segbuf = MagicMock()
            segbuf.snapshot_last.return_value = _make_segments(cfg)

            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"out")
                return MagicMock()

            with patch("src.video.processor.subprocess.run", side_effect=fake_run), patch(
                "src.video.processor.time.sleep"
            ), patch("src.video.processor.time.time", return_value=1700000000.0):
                out = build_highlight(cfg, segbuf)

            self.assertIsNotNone(out)
            self.assertEqual(len(calls), 2)
            final_cmd = calls[1]
            inputs = [final_cmd[i + 1] for i, token in enumerate(final_cmd) if token == "-i"]
            self.assertEqual(len(inputs), 1, "Modo sem watermark deve usar 1 input de vídeo")
            self.assertNotIn("-filter_complex", final_cmd)
            self.assertIn("-c", final_cmd)
            self.assertIn("copy", final_cmd)

    def test_build_highlight_missing_watermark_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = _make_cfg(base)
            segbuf = MagicMock()
            segbuf.snapshot_last.return_value = _make_segments(cfg)

            missing_wm = base / "files" / "missing.png"

            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"out")
                return MagicMock()

            with patch("src.video.processor.subprocess.run", side_effect=fake_run), patch(
                "src.video.processor.time.sleep"
            ), patch("src.video.processor.time.time", return_value=1700000000.0):
                out = build_highlight(
                    cfg,
                    segbuf,
                    watermark=WatermarkSpec(
                        path=str(missing_wm),
                        margin_px=24,
                        opacity=0.8,
                        rel_width=0.11,
                    ),
                )

            self.assertIsNone(out)
            self.assertEqual(
                len(calls),
                1,
                "Com watermark ausente, deve falhar antes do encode final",
            )
            err_files = list((cfg.failed_dir_highlight / "build_failed").glob("*.error.txt"))
            self.assertTrue(err_files, "Falha deve gerar arquivo .error.txt para diagnóstico")
            self.assertIn("Watermark inexistente", err_files[0].read_text())


if __name__ == "__main__":
    unittest.main()
