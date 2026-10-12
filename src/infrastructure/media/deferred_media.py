"""Bounded FFmpeg execution and atomic artifacts for deferred jobs."""

import math
import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from src.infrastructure.filesystem.deferred_repository import sync_directory
from src.video.processor import add_image_watermark, ffprobe_metadata


class DeferredMedia:
    def __init__(self, repository, storage):
        self.jobs = repository
        self.storage = storage

    def probe(self, path):
        metadata = ffprobe_metadata(Path(path))
        duration = metadata.get("duration_sec", 0)
        if not math.isfinite(duration) or duration <= 0 or not metadata.get("width"):
            raise ValueError("invalid media artifact")
        return metadata

    def run(self, command, *, job, lock_fd):
        duration = sum(
            s["media_end"] - s["media_start"] for s in job.details.get("segments", [])
        ) or job.details.get("duration_sec", 30)
        timeout = max(300, int(30 * duration))
        directory = self.jobs.directory(job.job_id)
        # The independent timeout and inherited flock also protect against Python dying.
        # No unbounded stderr pipe; diagnostics contain only a sanitized failure code.
        with subprocess.Popen(
            [
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                f"{timeout}s",
                "nice",
                "-n",
                "10",
                *command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(lock_fd,),
        ) as process:
            try:
                while process.poll() is None:
                    self.storage.check(directory)
                    time.sleep(1)
                if process.returncode:
                    raise RuntimeError("media_process_failed")
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise

    def _promote(self, temporary, target, expected_duration=None):
        metadata = self.probe(temporary)
        if expected_duration and abs(metadata["duration_sec"] - expected_duration) > max(
            0.25, expected_duration * 0.02
        ):
            raise ValueError("incomplete media output")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
        return metadata

    def concatenate(self, job, lock_fd):
        directory = self.jobs.directory(job.job_id)
        target = self.jobs.artifact(job, job.source_location)
        target.parent.mkdir(parents=True, exist_ok=True)
        listing = directory / "concat.txt"
        entries = []
        duration = 0
        for segment in job.details["segments"]:
            path = self.jobs.artifact(job, segment["path"])
            if path.stat().st_size != segment["size_bytes"]:
                raise ValueError("preserved segment size mismatch")
            self.probe(path)
            entries.append("file '" + segment["path"] + "'\n")
            duration += segment["media_end"] - segment["media_start"]
        listing.write_text("".join(entries))
        temporary = target.with_name("assembled.partial.mp4")
        self.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(listing),
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            job=job,
            lock_fd=lock_fd,
        )
        return self._promote(temporary, target, duration)

    def watermark(self, job, lock_fd):
        policy = job.details["policy"]
        target = self.jobs.artifact(job, "artifacts/final.mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("final.partial.mp4")

        def asset(key):
            return str(self.jobs.artifact(job, policy[key])) if policy.get(key) else None

        source = self.jobs.artifact(job, job.source_location)
        expected = self.probe(source)["duration_sec"]
        job = replace(job, details={**job.details, "duration_sec": expected})
        add_image_watermark(
            str(source),
            asset("watermark"),
            str(temporary),
            secondary_watermark_path=asset("client_watermark"),
            top_watermark_path=asset("top_watermark"),
            watermark_layout=policy.get("watermark_layout"),
            margin=policy["margin"],
            opacity=policy["opacity"],
            rel_width=policy["relative_width"],
            crf=policy["crf"],
            preset=policy["preset"],
            vertical_format=policy["vertical_format"],
            threads=1,
            runner=lambda cmd: self.run(cmd, job=job, lock_fd=lock_fd),
        )
        return self._promote(temporary, target, expected)

    def thumbnail(self, job, lock_fd):
        source = self.jobs.artifact(job, job.artifact_location)
        target = source.with_name("thumbnail.jpg")
        temporary = source.with_name("thumbnail.partial.jpg")
        duration = self.probe(source)["duration_sec"]
        job = replace(job, details={**job.details, "duration_sec": duration})
        self.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-threads",
                "1",
                "-ss",
                str(duration / 2),
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-q:v",
                "2",
                str(temporary),
            ],
            job=job,
            lock_fd=lock_fd,
        )
        if not temporary.is_file() or temporary.stat().st_size < 4:
            raise ValueError("thumbnail_incomplete")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
