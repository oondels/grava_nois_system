from __future__ import annotations

import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.video.processor import add_image_watermark, ffprobe_metadata


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

    def test_missing_client_logos_are_omitted_without_blocking_encoding(self):
        for bottom_exists, top_exists in ((False, False), (False, True), (True, False)):
            for vertical in (False, True):
                for layout_v2 in (False, True):
                    with self.subTest(bottom=bottom_exists, top=top_exists, vertical=vertical, layout=layout_v2), tempfile.TemporaryDirectory() as tmp:
                        base = Path(tmp)
                        inp, primary, bottom, top = (
                            base / name for name in ("in.mp4", "main.png", "bottom.png", "top.png")
                        )
                        inp.touch()
                        primary.touch()
                        if bottom_exists:
                            bottom.touch()
                        if top_exists:
                            top.touch()
                        layout = None
                        if layout_v2:
                            layout = {"placements": {
                                slot: {"x": x, "y": y, "width": .2, "height": .15,
                                       "opacity": .8, "imageWidth": 200, "imageHeight": 100}
                                for slot, x, y in (
                                    ("institutional", .4, .8),
                                    ("clientBottom", .02, .8),
                                    ("clientTop", .02, .02),
                                )
                            }}
                        with patch("src.video.processor.ffprobe_metadata", return_value={"width": 1280, "height": 720}), patch(
                            "src.video.processor.subprocess.run"
                        ) as run, patch("src.video.processor.logger.warning") as warning:
                            add_image_watermark(
                                str(inp), str(primary), str(base / "out.mp4"),
                                secondary_watermark_path=str(bottom),
                                top_watermark_path=str(top),
                                vertical_format=vertical,
                                watermark_layout=layout,
                            )
                        run.assert_called_once()
                        self.assertEqual(warning.call_count, int(not bottom_exists) + int(not top_exists))
                        cmd = run.call_args.args[0]
                        inputs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-i"]
                        expected = [str(inp), str(primary)]
                        if bottom_exists:
                            expected.append(str(bottom))
                        if top_exists:
                            expected.append(str(top))
                        self.assertEqual(inputs, expected)
                        filt = cmd[cmd.index("-filter_complex") + 1]
                        self.assertIn("[1:v]scale=", filt)
                        self.assertEqual(filt.count("overlay="), len(expected) - 1)
                        self.assertTrue(filt.endswith("[v]"))
                        self.assertEqual("crop=" in filt, vertical)
                        self.assertNotIn("[3:v]", filt)
                        if bottom_exists or top_exists:
                            self.assertIn("[2:v]scale=", filt)
                        else:
                            self.assertNotIn("[2:v]", filt)

    def test_missing_primary_logo_still_fails_before_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inp, primary = base / "in.mp4", base / "main.png"
            inp.touch()
            with self.assertRaisesRegex(FileNotFoundError, "Watermark inexistente"), patch("src.video.processor.subprocess.run") as run:
                add_image_watermark(str(inp), str(primary), str(base / "out.mp4"))
            run.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_video_is_generated_without_client_images(self):
        primary = Path(__file__).resolve().parents[1] / "files" / "replay_grava_nois_wm.png"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=1",
                 "-c:v", "libx264", "-threads", "1", str(source)],
                check=True, capture_output=True,
            )
            for vertical in (False, True):
                with self.subTest(vertical=vertical):
                    output = base / f"out-{vertical}.mp4"
                    add_image_watermark(
                        str(source), str(primary), str(output),
                        secondary_watermark_path=str(base / "missing-bottom.png"),
                        top_watermark_path=str(base / "missing-top.png"),
                        vertical_format=vertical, threads=1, preset="ultrafast",
                    )
                    metadata = ffprobe_metadata(output)
                    self.assertEqual(metadata["height"], 360)
                    self.assertEqual(metadata["width"], 202 if vertical else 640)
                    self.assertAlmostEqual(metadata["duration_sec"], 1, places=1)


if __name__ == "__main__":
    unittest.main()
