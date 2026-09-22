"""Isolated deferred edge contracts: no cameras, broker, API or credentials."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from src.application.replay.preserve_replay import PreserveReplay
from src.config.settings import CaptureConfig
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob
from src.domain.delivery import ClipJobState as State
from src.domain.replay.processing_schedule import (
    DeviceActivity,
    validate_windows,
    window_authorization,
)
from src.infrastructure.filesystem.deferred_repository import (
    DeferredJobRepository,
    LockBusy,
    durable_copy,
    exclusive_file,
    safe_child,
)
from src.video.buffer import SegmentBuffer

UTC = UTC


IDENTITY = {"device_id": "test-device", "client_id": "test-client", "venue_id": "test-venue"}


class ScheduleTests(unittest.TestCase):
    def test_mandatory_boundaries_and_empty_additional_windows(self):
        for hour, minute, allowed in ((23, 59, False), (0, 0, True), (4, 59, True), (5, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(
                    allowed,
                    bool(
                        window_authorization(
                            datetime(2026, 9, 21, hour, minute, tzinfo=UTC), "UTC", []
                        )
                    ),
                )

    def test_timezone_is_existing_installation_timezone(self):
        now = datetime(2026, 9, 21, 7, 59, tzinfo=UTC)
        self.assertTrue(window_authorization(now, "America/Sao_Paulo", []))
        self.assertFalse(window_authorization(now, "UTC", []))

    def test_multiple_overlap_and_crossing_sunday(self):
        windows = [
            {"weekdays": [7], "start": "22:00", "end": "08:00"},
            {"weekdays": [1], "start": "07:00", "end": "09:00"},
        ]
        for hour, expected in ((7, True), (8, True), (9, False)):
            self.assertEqual(
                expected,
                bool(window_authorization(datetime(2026, 9, 21, hour, tzinfo=UTC), "UTC", windows)),
            )
        self.assertFalse(window_authorization(datetime(2026, 9, 22, 7, tzinfo=UTC), "UTC", windows))

    def test_invalid_windows_do_not_disable_mandatory_rule(self):
        for windows in (
            {},
            [{"weekdays": [True], "start": "01:00", "end": "02:00"}],
            [{"weekdays": [1], "start": "08:00", "end": "08:00"}],
            [{"weekdays": [1], "start": "24:00", "end": "08:00"}],
        ):
            self.assertTrue(validate_windows(windows))

    def test_idle_boundary_restart_and_activity_does_not_follow_wallclock(self):
        mono = [100.0]
        activity = DeviceActivity(lambda: mono[0])
        now = datetime(2026, 9, 21, 12, tzinfo=UTC)
        mono[0] += 1799
        self.assertIsNone(activity.authorization(now, "UTC", []))
        mono[0] += 1
        self.assertEqual("idle", activity.authorization(now + timedelta(days=2), "UTC", []))
        self.assertEqual("idle", activity.authorization(now, "UTC", []))
        activity.record()  # any valid trigger, even if preservation fails
        self.assertIsNone(activity.authorization(now, "UTC", []))
        self.assertIsNone(DeviceActivity(lambda: mono[0]).authorization(now, "UTC", []))


class RepositoryTests(unittest.TestCase):
    def test_lock_excludes_other_consumers_and_releases_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "heavy.lock"
            with exclusive_file(lock), self.assertRaises(LockBusy), exclusive_file(lock):
                pass
            with exclusive_file(lock):
                pass

    def test_manifest_roundtrip_restart_and_path_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DeferredJobRepository(Path(tmp))
            job = ClipJob(
                "j1",
                CameraId("cam"),
                "artifacts/assembled.mp4",
                datetime.now(UTC),
                state=State.PREPARING,
                schema_version=3,
                details={"captured_at": "original", "segments": []},
            )
            repo.save(job)
            self.assertEqual("original", repo.get("j1").details["captured_at"])
            repo.recover()
            self.assertEqual(State.FAILED, repo.get("j1").state)
            for path in ("../other", "/tmp/other"):
                with self.assertRaises(ValueError):
                    safe_child(Path(tmp), path)
            (Path(tmp) / "link").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                safe_child(Path(tmp), "link/data")

    def test_unknown_schema_and_corrupt_manifest_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DeferredJobRepository(Path(tmp))
            root = repo.directory("future")
            root.mkdir()
            (root / "manifest.json").write_text('{"schema_version":99}')
            self.assertEqual((), repo.all())
            self.assertEqual(1, repo.invalid_count())
            self.assertTrue((root / "manifest.json").exists())


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = CaptureConfig(
            "cam",
            self.root / "buffer",
            self.root / "clips",
            self.root / "queue",
            self.root / "failed",
            pre_seconds=1,
            post_seconds=1,
            max_buffer_seconds=2,
            track_segments=True,
            capture_session_id="session",
        )
        self.cfg.ensure_dirs()
        self.cfg.segment_list_path = self.cfg.buffer_dir / "closed.csv"
        self.buffer = SegmentBuffer(self.cfg)
        self.buffer._media_origin = 100.0
        self.events = Mock()
        self.storage = Mock()
        self.jobs = DeferredJobRepository(self.root / "jobs")
        self.now = [101.0]
        self.preserve = PreserveReplay(
            self.jobs,
            lambda src, dst: durable_copy(Path(src), dst),
            self.storage,
            self.events,
            lambda: self.now[0],
        )

    def segments(self, count):
        lines = []
        for n in range(count):
            name = f"buffer{n:06d}.ts"
            (self.cfg.buffer_dir / name).write_bytes(b"closed")
            lines.append(f"{name},{n},{n + 1}\n")
        self.cfg.segment_list_path.write_text("".join(lines))
        self.buffer._index_closed()

    def begin(self):
        return self.preserve.begin(
            self.cfg,
            self.buffer,
            trigger_id="trigger",
            captured_at="2026-09-21T12:00:00+00:00",
            triggered_mono=101,
            identity=IDENTITY,
            policy={"assets_saved": True},
        )

    def test_prebuffer_protected_before_postbuffer_and_overlapping_jobs(self):
        self.segments(1)
        first, second = self.begin(), self.begin()
        self.segments(4)  # old pre segment would normally have been evicted
        self.assertTrue((self.cfg.buffer_dir / "buffer000000.ts").exists())
        self.preserve.tick()
        for job_id in (first, second):
            job = self.jobs.get(job_id)
            self.assertEqual(State.QUEUED, job.state)
            self.assertEqual(2, len(job.details["segments"]))
            self.assertTrue(self.jobs.artifact(job, job.details["segments"][0]["path"]).exists())
        self.buffer._index_closed()
        self.assertFalse((self.cfg.buffer_dir / "buffer000000.ts").exists())

    def test_open_segment_is_not_copied_and_timeout_is_failed(self):
        self.segments(1)
        (self.cfg.buffer_dir / "buffer000001.ts").write_bytes(b"still open")
        job_id = self.begin()
        self.preserve.tick()
        self.assertEqual(State.PREPARING, self.jobs.get(job_id).state)
        self.assertEqual(1, len(self.jobs.get(job_id).details["segments"]))
        self.now[0] = 200
        self.preserve.tick()
        self.assertEqual(State.FAILED, self.jobs.get(job_id).state)
        self.assertFalse(self.preserve.pending)

    def test_unreadable_manifest_releases_pins_without_blocking_other_jobs(self):
        self.segments(1)
        broken, healthy = self.begin(), self.begin()
        (self.jobs.directory(broken) / "manifest.json").write_text("invalid")
        self.segments(3)
        self.preserve.tick()
        self.assertNotIn(broken, self.preserve.pending)
        self.assertNotIn(broken, self.buffer._reservations)
        self.assertEqual(State.QUEUED, self.jobs.get(healthy).state)

    def test_stopped_capture_retains_pinned_session_until_preservation_finishes(self):
        self.segments(1)
        job_id = self.begin()
        old = self.cfg.buffer_dir / "buffer-session-000000.ts"
        old.write_bytes(b"closed")
        self.buffer.stop()
        self.assertTrue(old.exists())
        self.preserve.tick()
        self.assertEqual(State.FAILED, self.jobs.get(job_id).state)
        self.assertFalse(old.exists())

    def test_actual_durations_and_gaps_define_eligibility(self):
        segment = {"start_offset": -1, "end_offset": 2, "session_id": "a"}
        self.assertTrue(PreserveReplay._complete([segment], 2))
        self.assertFalse(PreserveReplay._complete([segment], 4))
        self.assertFalse(
            PreserveReplay._complete(
                [segment, {"start_offset": 3, "end_offset": 5, "session_id": "a"}], 4
            )
        )


if __name__ == "__main__":
    unittest.main()
