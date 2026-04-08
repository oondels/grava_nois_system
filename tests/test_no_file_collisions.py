from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.config_loader import reset_config_cache
from src.config.settings import CaptureConfig, load_capture_configs
from src.video.processor import build_highlight


def _make_cfg(camera_id: str, base: Path) -> CaptureConfig:
    return CaptureConfig(
        camera_id=camera_id,
        buffer_dir=base / "buffer" / camera_id,
        clips_dir=base / "clips" / camera_id,
        queue_dir=base / "queue" / camera_id,
        failed_dir_highlight=base / "failed" / camera_id,
        seg_time=1,
        pre_seconds=5,
        post_seconds=3,
    )


def _run_build_highlight(cfg: CaptureConfig, fixed_ts: float = 1700000000.0) -> Path | None:
    """Run build_highlight with all external dependencies mocked."""
    segs = []
    for i in range(10):
        p = cfg.buffer_dir / f"seg{i:03d}.ts"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 100)
        segs.append(str(p))

    segbuf = MagicMock()
    segbuf.snapshot_last.return_value = segs

    def fake_run(cmd, **kwargs):
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.touch()
        return MagicMock()

    import src.video.processor as proc_mod

    with patch.object(proc_mod.time, "sleep"), \
         patch.object(proc_mod.time, "time", return_value=fixed_ts), \
         patch("src.video.processor.subprocess.run", side_effect=fake_run):
        cfg.clips_dir.mkdir(parents=True, exist_ok=True)
        cfg.failed_dir_highlight.mkdir(parents=True, exist_ok=True)
        return build_highlight(cfg, segbuf)


class FileCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()

    def test_multi_camera_configs_have_isolated_dirs(self) -> None:
        """Cameras loaded from GN_CAMERAS_JSON use separate subdirectories."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "GN_CAMERAS_JSON": (
                        '[{"id":"cam01","rtsp_url":"rtsp://10.0.0.1/live","enabled":true},'
                        '{"id":"cam02","rtsp_url":"rtsp://10.0.0.2/live","enabled":true}]'
                    )
                },
                clear=False,
            ):
                os.environ.pop("GN_RTSP_URLS", None)
                os.environ.pop("GN_RTSP_URL", None)
                cfgs = load_capture_configs(base=base, seg_time=1)

        self.assertEqual(len(cfgs), 2)
        # All key directories must be distinct between cameras
        for attr in ("buffer_dir", "clips_dir", "queue_dir", "failed_dir_highlight"):
            dirs = [str(getattr(c, attr)) for c in cfgs]
            self.assertEqual(len(set(dirs)), 2, f"{attr} collision detected")

    def test_filename_includes_camera_id(self) -> None:
        """build_highlight output filename must contain the camera_id."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg("cam01", Path(tmp))
            result = _run_build_highlight(cfg)

        self.assertIsNotNone(result)
        self.assertIn("cam01", result.name)

    def test_two_cameras_same_timestamp_different_paths(self) -> None:
        """Cameras with identical timestamps produce different filenames due to camera_id."""
        fixed_ts = 1700000000.0  # exact same second, same microsecond
        results = []

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for cam_id in ("cam01", "cam02"):
                cfg = _make_cfg(cam_id, base)
                result = _run_build_highlight(cfg, fixed_ts=fixed_ts)
                self.assertIsNotNone(result, f"{cam_id} build returned None")
                results.append(result)

        self.assertNotEqual(results[0], results[1])
        self.assertIn("cam01", results[0].name)
        self.assertIn("cam02", results[1].name)

    def test_microsecond_timestamp_in_filename(self) -> None:
        """Filename timestamp format uses microsecond precision."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg("cam99", Path(tmp))
            result = _run_build_highlight(cfg, fixed_ts=1700000000.123456)

        self.assertIsNotNone(result)
        # %f gives 6-digit microseconds – filename must contain them
        self.assertRegex(result.name, r"highlight_cam99_\d{8}-\d{6}-\d{6}Z\.mp4")


if __name__ == "__main__":
    unittest.main()
