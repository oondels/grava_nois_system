from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.config.settings import CaptureConfig
from src.domain.configuration import ProcessingPolicy, RtspSnapshot, V4l2Snapshot
from src.infrastructure.media import (
    ConfiguredLegacyMediaToolAdapter,
    FfmpegCaptureCommandBuilder,
    WatermarkPolicy,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _capture(base: Path, *, source_type: str = "rtsp") -> CaptureConfig:
    return CaptureConfig(
        camera_id="cam01",
        camera_name="cam01",
        source_type=source_type,
        rtsp_url="rtsp://user:pass@192.168.1.20:554/stream1",
        device="/dev/video-test",
        buffer_dir=base / "buffer",
        clips_dir=base / "clips",
        queue_dir=base / "queue",
        failed_dir_highlight=base / "failed",
        seg_time=1,
    )


def _rtsp(**overrides: object) -> RtspSnapshot:
    values: dict[str, object] = {
        "max_retries": 3,
        "timeout_seconds": 5,
        "startup_check_seconds": 2,
        "reencode": None,
        "fps": "",
        "gop": 30,
        "preset": "veryfast",
        "crf": 23,
        "use_wallclock_timestamps": False,
        "profile": None,
        "low_latency_input": False,
        "low_delay_codec_flags": False,
    }
    values.update(overrides)
    return RtspSnapshot(**values)  # type: ignore[arg-type]


V4L2 = V4l2Snapshot(device="/dev/video0", framerate=30, video_size="1280x720")


class FfmpegCaptureCommandBuilderTests(unittest.TestCase):
    def test_default_rtsp_command_matches_characterized_legacy_contract(self) -> None:
        fixture = json.loads(
            (
                PROJECT_ROOT / "tests" / "characterization" / "fixtures" / "legacy_contracts.json"
            ).read_text(encoding="utf-8")
        )["rtsp_hq_command"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            command = FfmpegCaptureCommandBuilder().build(
                capture=_capture(base),
                rtsp=_rtsp(),
                v4l2=V4L2,
                processing=ProcessingPolicy(light_mode=False),
                segment_start_number=0,
            )
            normalized = [
                "<RTSP_URL>"
                if item.startswith("rtsp://")
                else item.replace(str(base / "buffer"), "<BUFFER>")
                for item in command
            ]
        self.assertEqual(fixture, normalized)

    def test_compatible_profile_reproduces_reencode_tuning_and_input_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = FfmpegCaptureCommandBuilder().build(
                capture=_capture(Path(tmp)),
                rtsp=_rtsp(
                    profile="compatible",
                    fps="15",
                    gop=45,
                    preset="ultrafast",
                    crf=20,
                    use_wallclock_timestamps=True,
                    low_latency_input=True,
                    low_delay_codec_flags=True,
                ),
                v4l2=V4L2,
                processing=ProcessingPolicy(),
                segment_start_number=9,
            )
        self.assertEqual("libx264", command[command.index("-c:v") + 1])
        self.assertEqual("fps=15", command[command.index("-vf") + 1])
        self.assertEqual("45", command[command.index("-g") + 1])
        self.assertEqual("9", command[command.index("-segment_start_number") + 1])
        self.assertLess(command.index("-use_wallclock_as_timestamps"), command.index("-i"))
        self.assertIn("nobuffer", command)
        self.assertIn("low_delay", command)

    def test_explicit_reencode_wins_over_profile_and_light_mode_infers_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = _capture(Path(tmp))
            explicit_copy = FfmpegCaptureCommandBuilder().build(
                capture=capture,
                rtsp=_rtsp(profile="compatible", reencode=False),
                v4l2=V4L2,
                processing=ProcessingPolicy(light_mode=True),
                segment_start_number=0,
            )
            inferred_reencode = FfmpegCaptureCommandBuilder().build(
                capture=capture,
                rtsp=_rtsp(),
                v4l2=V4L2,
                processing=ProcessingPolicy(light_mode=True),
                segment_start_number=0,
            )
        self.assertEqual("copy", explicit_copy[explicit_copy.index("-c:v") + 1])
        self.assertEqual(
            "libx264",
            inferred_reencode[inferred_reencode.index("-c:v") + 1],
        )

    def test_v4l2_command_matches_legacy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = FfmpegCaptureCommandBuilder().build(
                capture=_capture(Path(tmp), source_type="v4l2"),
                rtsp=_rtsp(),
                v4l2=V4L2,
                processing=ProcessingPolicy(),
                segment_start_number=3,
            )
        self.assertEqual("v4l2", command[command.index("-f") + 1])
        self.assertEqual("/dev/video-test", command[command.index("-i") + 1])
        self.assertEqual("30", command[command.index("-framerate") + 1])
        self.assertEqual("1280x720", command[command.index("-video_size") + 1])
        self.assertEqual("0", command[command.index("-reset_timestamps") + 1])

    def test_rejects_unresolved_or_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            capture = _capture(base)
            capture.rtsp_url = None
            with self.assertRaises(ValueError):
                FfmpegCaptureCommandBuilder().build(
                    capture=capture,
                    rtsp=_rtsp(),
                    v4l2=V4L2,
                    processing=ProcessingPolicy(),
                    segment_start_number=0,
                )
            capture.source_type = "unknown"
            with self.assertRaises(ValueError):
                FfmpegCaptureCommandBuilder().build(
                    capture=capture,
                    rtsp=_rtsp(),
                    v4l2=V4L2,
                    processing=ProcessingPolicy(),
                    segment_start_number=0,
                )


class ConfiguredLegacyMediaToolAdapterTests(unittest.TestCase):
    def test_binds_resolved_watermark_policy_and_converts_probe_path(self) -> None:
        concatenate = Mock()
        watermark = Mock()
        probe = Mock(return_value={"duration_sec": 4.5})
        policy = WatermarkPolicy(
            image_path="/branding/main.png",
            secondary_image_path="/branding/client.png",
            margin=18,
            opacity=0.7,
            relative_width=0.2,
            secondary_relative_width=0.12,
            codec="h264_v4l2m2m",
            crf=21,
            preset="fast",
            vertical_format=True,
        )
        media = ConfiguredLegacyMediaToolAdapter(
            concatenate=concatenate,
            watermark=watermark,
            probe=probe,
            policy=policy,
        )

        media.concatenate(("one.ts", "two.ts"), "replay.mp4")
        media.apply_watermark("replay.mp4", "watermarked.mp4")
        result = media.probe("watermarked.mp4")

        concatenate.assert_called_once_with(("one.ts", "two.ts"), "replay.mp4")
        watermark.assert_called_once_with(
            "replay.mp4",
            "/branding/main.png",
            "watermarked.mp4",
            secondary_watermark_path="/branding/client.png",
            margin=18,
            opacity=0.7,
            rel_width=0.2,
            secondary_rel_width=0.12,
            codec="h264_v4l2m2m",
            crf=21,
            preset="fast",
            vertical_format=True,
        )
        probe.assert_called_once_with(Path("watermarked.mp4"))
        self.assertEqual({"duration_sec": 4.5}, result)

    def test_watermark_policy_validates_all_ranges(self) -> None:
        base = {"image_path": "logo.png"}
        for override in (
            {"image_path": " "},
            {"margin": -1},
            {"opacity": -0.1},
            {"opacity": 1.1},
            {"relative_width": 0},
            {"secondary_relative_width": 2},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                WatermarkPolicy(**(base | override))


if __name__ == "__main__":
    unittest.main()
