from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.video.processor import add_image_watermark


class DualWatermarkCommandTests(unittest.TestCase):
    def test_add_image_watermark_uses_two_logo_inputs_when_secondary_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp = base / "in.mp4"
            wm1 = base / "wm1.png"
            wm2 = base / "wm2.png"
            out = base / "out.mp4"
            inp.write_bytes(b"video")
            wm1.write_bytes(b"png")
            wm2.write_bytes(b"png")

            calls: list[list[str]] = []

            def _fake_run(cmd, **kwargs):
                calls.append(cmd)
                return MagicMock()

            with patch("src.video.processor.ffprobe_metadata", return_value={"width": 1280}), patch(
                "src.video.processor.subprocess.run", side_effect=_fake_run
            ):
                add_image_watermark(
                    input_path=str(inp),
                    watermark_path=str(wm1),
                    secondary_watermark_path=str(wm2),
                    output_path=str(out),
                    margin=24,
                    opacity=0.8,
                    rel_width=0.11,
                    preset="veryfast",
                )

            self.assertEqual(len(calls), 1)
            cmd = calls[0]
            inputs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-i"]
            self.assertEqual(len(inputs), 3)
            self.assertEqual(inputs[0], str(inp))
            self.assertEqual(inputs[1], str(wm1))
            self.assertEqual(inputs[2], str(wm2))
            filt = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("[0:v][wm1]overlay=", filt)
            self.assertIn("[v1][wm2]overlay=", filt)
            self.assertIn("[v1][wm2]overlay=x=24:y=main_h-overlay_h-24[v]", filt)

    def test_top_logo_without_secondary_uses_correct_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp, primary, top = (base / name for name in ("in.mp4", "main.png", "top.png"))
            for path in (inp, primary, top):
                path.write_bytes(b"dummy")
            with patch("src.video.processor.ffprobe_metadata", return_value={"width": 1280}), patch(
                "src.video.processor.subprocess.run"
            ) as run:
                add_image_watermark(str(inp), str(primary), str(base / "out.mp4"), top_watermark_path=str(top))
            cmd = run.call_args.args[0]
            inputs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-i"]
            self.assertEqual(inputs, [str(inp), str(primary), str(top)])
            filt = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("[2:v]scale=161:-1", filt)
            self.assertIn("[v1][wm_top]overlay=x=24:y=24[v]", filt)

    def test_missing_top_logo_fails_before_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp, primary = base / "in.mp4", base / "main.png"
            inp.touch()
            primary.touch()
            with self.assertRaises(FileNotFoundError), patch("src.video.processor.subprocess.run") as run:
                add_image_watermark(str(inp), str(primary), str(base / "out.mp4"), top_watermark_path=str(base / "missing.png"))
            run.assert_not_called()

    def test_add_image_watermark_fails_when_secondary_logo_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp = base / "in.mp4"
            wm1 = base / "wm1.png"
            missing = base / "missing.png"
            out = base / "out.mp4"
            inp.write_bytes(b"video")
            wm1.write_bytes(b"png")

            with self.assertRaises(FileNotFoundError):
                add_image_watermark(
                    input_path=str(inp),
                    watermark_path=str(wm1),
                    secondary_watermark_path=str(missing),
                    output_path=str(out),
                )


if __name__ == "__main__":
    unittest.main()
