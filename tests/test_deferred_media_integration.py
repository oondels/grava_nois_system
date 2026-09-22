"""Real FFmpeg against generated local media; never opens a camera or network."""

import csv
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState
from src.infrastructure.filesystem.deferred_repository import DeferredJobRepository, exclusive_file
from src.infrastructure.media.deferred_media import DeferredMedia


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("timeout"),
    "FFmpeg/coreutils unavailable",
)
class DeferredMediaIntegrationTests(unittest.TestCase):
    def test_closed_csv_segments_concat_watermark_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = DeferredJobRepository(root / "queue")
            directory = jobs.directory("synthetic")
            segments_dir = directory / "segments"
            segments_dir.mkdir(parents=True)
            csv_path = directory / "closed.csv"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10",
                    "-t",
                    "3",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-g",
                    "20",
                    "-keyint_min",
                    "20",
                    "-sc_threshold",
                    "0",
                    "-f",
                    "segment",
                    "-segment_time",
                    "1",
                    "-segment_list",
                    str(csv_path),
                    "-segment_list_type",
                    "csv",
                    "-reset_timestamps",
                    "1",
                    str(segments_dir / "buffer%06d.ts"),
                ],
                check=True,
                timeout=30,
            )
            segments = [
                {
                    "path": "segments/" + name,
                    "media_start": float(start),
                    "media_end": float(end),
                    "size_bytes": (segments_dir / name).stat().st_size,
                }
                for name, start, end in csv.reader(csv_path.read_text().splitlines())
            ]
            self.assertGreater(segments[0]["media_end"] - segments[0]["media_start"], 1)
            assets = directory / "assets"
            assets.mkdir()
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=white:s=16x16",
                    "-frames:v",
                    "1",
                    "-threads",
                    "1",
                    str(assets / "logo.png"),
                ],
                check=True,
                timeout=30,
            )
            job = ClipJob(
                "synthetic",
                CameraId("cam"),
                "artifacts/assembled.mp4",
                datetime.now(UTC),
                schema_version=3,
                details={
                    "segments": segments,
                    "policy": {
                        "watermark": "assets/logo.png",
                        "margin": 2,
                        "opacity": 0.8,
                        "relative_width": 0.15,
                        "crf": 26,
                        "preset": "ultrafast",
                        "vertical_format": False,
                    },
                },
            )
            jobs.save(job)
            media = DeferredMedia(jobs, Mock())
            with exclusive_file(root / "heavy.lock") as lock:
                metadata = media.concatenate(job, lock)
                self.assertGreater(metadata["duration_sec"], 2.5)
                media.watermark(job, lock)
                job = replace(
                    job, state=ClipJobState.WATERMARKED, artifact_location="artifacts/final.mp4"
                )
                media.thumbnail(job, lock)
            self.assertTrue((directory / "artifacts/thumbnail.jpg").is_file())
            self.assertTrue((directory / "artifacts/final.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
