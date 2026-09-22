"""Isolated deferred edge contracts: no cameras, broker, API or credentials."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.application.delivery.deferred_coordinator import DeferredCoordinator
from src.application.replay.preserve_replay import PreserveReplay
from src.bootstrap.deferred_runtime import DeferredArtifacts
from src.config.config_loader import OperationalConfig
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
    DeferredLeases,
    LockBusy,
    durable_copy,
    exclusive_file,
    safe_child,
)
from src.infrastructure.http.deferred_gateway import DeferredVideoGateway
from src.services.mqtt.operational_event_service import OperationalEventService, sign_operational
from src.services.storage_monitor import StorageBlocked, StorageMonitor
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


class StorageTests(unittest.TestCase):
    def test_decimal_threshold_hysteresis_and_reservations(self):
        with tempfile.TemporaryDirectory() as tmp:
            free = [4_000_000_000]
            events = Mock()
            monitor = StorageMonitor(
                [tmp, tmp], events, lambda _: SimpleNamespace(free=free[0], total=10_000_000_000)
            )
            self.assertFalse(monitor.poll()[0]["low_space"])
            free[0] -= 1
            self.assertTrue(monitor.poll()[0]["low_space"])
            monitor.poll()
            self.assertEqual(1, events.emit.call_count)
            monitor.check(Path(tmp), 1024)  # warning alone does not block
            free[0] = 4_500_000_000
            self.assertTrue(monitor.poll()[0]["low_space"])
            self.assertFalse(monitor.poll()[0]["low_space"])
            monitor.reserve("a", Path(tmp), 4_000_000_000)
            with self.assertRaises(StorageBlocked):
                monitor.reserve("b", Path(tmp), 1_000_000_000)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Mock()
        self.client.publish_json.return_value = True
        self.service = OperationalEventService(
            Path(self.tmp.name),
            self.client,
            lambda suffix: "grn/devices/test/" + suffix,
            IDENTITY,
            "test-only-secret",
        )

    def ack(self, payload, status="persisted"):
        ack = {
            **IDENTITY,
            "event_id": payload["event_id"],
            "ack_hash": payload["content_hash"],
            "status": status,
        }
        ack["signature"] = sign_operational(ack, "test-only-secret")
        self.service._ack("grn/devices/test/capture/events/ack", json.dumps(ack).encode())

    def test_publish_success_is_not_backend_confirmation(self):
        self.service.emit("processing.failed", code="test_error")
        self.service.flush_once()
        path = next(self.service.outbox.glob("*.json"))
        payload = json.loads(path.read_text())
        self.ack(payload, "rejected")
        self.assertTrue(path.exists())
        self.ack(payload)
        self.assertFalse(path.exists())
        self.assertEqual(1, len(list(self.service.history.glob("*.json"))))

    def test_restart_keeps_identity_and_failed_network_pending(self):
        self.service.emit("processing.failed", code="test_error")
        path = next(self.service.outbox.glob("*.json"))
        original = json.loads(path.read_text())
        other = OperationalEventService(
            Path(self.tmp.name), self.client, self.service.topic_for, IDENTITY, "test-only-secret"
        )
        self.client.publish_json.return_value = False
        other.flush_once()
        self.assertEqual(original, json.loads(path.read_text()))

    def test_saturation_is_persisted_and_no_fake_success(self):
        self.service.OUTBOX_LIMIT = 10
        self.service.CRITICAL_RESERVE = 0
        self.service.emit("processing.failed", code="test_error")
        self.assertTrue(json.loads(self.service.status_path.read_text())["saturated"])
        self.assertEqual([], list(self.service.outbox.glob("*.json")))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = DeferredJobRepository(self.root / "jobs")
        self.job = ClipJob(
            "job",
            CameraId("cam"),
            "artifacts/assembled.mp4",
            datetime.now(UTC),
            state=State.WATERMARKED,
            artifact_location="artifacts/final.mp4",
            schema_version=3,
            details={
                **IDENTITY,
                "captured_at": "2026-09-21T12:00:00+00:00",
                "thumbnail_complete": True,
                "policy": {"max_attempts": 3},
                "source_kind": "segments",
                "segments": [],
            },
        )
        self.repo.save(self.job)
        artifact = self.repo.artifact(self.job, self.job.artifact_location)
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"final-video")
        self.client = Mock(**IDENTITY)
        for key, value in IDENTITY.items():
            setattr(self.client, key, value)
        self.client.is_configured.return_value = True
        self.client.register_clip_metadados.return_value = {
            "data": {"clip": {"clip_id": "remote", "upload_url": "https://test.invalid/upload"}}
        }
        from src.services.api_client import GravaNoisAPIClient

        self.client.extract_clip_registration.side_effect = (
            GravaNoisAPIClient.extract_clip_registration
        )
        self.client.upload_file_to_signed_url.return_value = (200, "OK", {"etag": "receipt"})
        self.client.finalize_clip_uploaded.return_value = {
            "data": {"clip_id": "remote", "status": "uploaded"}
        }
        self.events = Mock()
        self.media = Mock()
        self.config = OperationalConfig()
        self.coordinator = DeferredCoordinator(
            self.repo,
            self.media,
            DeferredVideoGateway(self.repo, self.client),
            DeferredArtifacts(self.repo, []),
            DeferredLeases(self.repo),
            lambda: exclusive_file(self.root / "heavy.lock"),
            DeviceActivity(),
            Mock(),
            self.events,
            lambda: self.config,
            SimpleNamespace(pending={}),
        )

    def deliver(self):
        with patch(
            "src.video.processor.ffprobe_metadata", return_value={"duration_sec": 10, "width": 100}
        ):
            self.coordinator.deliver_once()

    def due(self):
        job = self.repo.get("job")
        self.repo.save(replace(job, next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))

    def test_upload_retry_reuses_final_without_media(self):
        self.client.upload_file_to_signed_url.return_value = (503, "unavailable", {})
        self.deliver()
        self.assertEqual(State.RETRY_PENDING, self.repo.get("job").state)
        self.assertTrue(self.repo.artifact(self.job, self.job.artifact_location).exists())
        self.due()
        self.client.upload_file_to_signed_url.return_value = (200, "OK", {})
        self.deliver()
        self.assertEqual(State.FINALIZED, self.repo.get("job").state)
        self.media.watermark.assert_not_called()
        self.assertEqual(2, self.client.register_clip_metadados.call_count)

    def test_finalize_retry_does_not_repeat_upload_or_registration(self):
        self.client.finalize_clip_uploaded.side_effect = TimeoutError()
        self.deliver()
        job = self.repo.get("job")
        self.assertEqual(State.UPLOADED, job.retry_from)
        self.assertEqual("remote", job.remote_clip_id)
        self.due()
        self.client.finalize_clip_uploaded.side_effect = None
        self.deliver()
        self.assertEqual(1, self.client.upload_file_to_signed_url.call_count)
        self.assertEqual(1, self.client.register_clip_metadados.call_count)
        self.assertEqual(State.FINALIZED, self.repo.get("job").state)

    def test_cleanup_failure_preserves_finalization_and_does_not_repeat_upload(self):
        actual = self.coordinator.artifacts.cleanup
        self.coordinator.artifacts.cleanup = Mock(side_effect=OSError("disk busy"))
        self.deliver()
        self.assertEqual(State.FINALIZED, self.repo.get("job").state)
        self.assertEqual("cleanup_pending", self.repo.get("job").details["last_error"]["code"])
        self.due()
        self.coordinator.artifacts.cleanup = actual
        self.deliver()
        self.assertTrue(self.repo.get("job").details["cleaned"])
        self.assertEqual(1, self.client.finalize_clip_uploaded.call_count)
        self.assertEqual(1, self.client.upload_file_to_signed_url.call_count)

    def test_expired_signed_url_refreshes_registration_with_same_identity(self):
        self.client.upload_file_to_signed_url.return_value = (403, "ExpiredToken", {})
        self.deliver()
        job = self.repo.get("job")
        self.assertEqual(State.REGISTERED, job.retry_from)
        self.assertEqual("remote", job.remote_clip_id)
        self.due()
        self.client.upload_file_to_signed_url.return_value = (200, "OK", {})
        self.deliver()
        self.assertEqual(State.FINALIZED, self.repo.get("job").state)
        self.assertEqual(2, self.client.register_clip_metadados.call_count)

    def test_dev_preserves_without_remote_calls(self):
        self.repo.save(
            replace(
                self.job, details={**self.job.details, "policy": {"max_attempts": 3, "dev": True}}
            )
        )
        self.deliver()
        self.assertEqual(State.DEV_PRESERVED, self.repo.get("job").state)
        self.client.register_clip_metadados.assert_not_called()
        self.assertTrue(self.repo.artifact(self.job, self.job.artifact_location).exists())

    def test_schedule_never_blocks_ready_upload(self):
        self.config.operation_window.time_zone = "UTC"
        self.deliver()
        self.assertEqual(State.FINALIZED, self.repo.get("job").state)

    def test_ingest_time_error_blocks_without_deleting_or_using_attempts(self):
        import requests

        response = requests.Response()
        response.status_code = 403
        response._content = b'{"error":"request_outside_allowed_time_window","message":"outside"}'
        self.client.register_clip_metadados.side_effect = requests.HTTPError(response=response)
        self.deliver()
        job = self.repo.get("job")
        self.assertEqual(State.BLOCKED, job.state)
        self.assertEqual(0, job.attempts)
        self.assertTrue(self.repo.artifact(job, job.artifact_location).exists())

    def test_authentication_error_is_terminal_and_preserves_artifact(self):
        import requests

        response = requests.Response()
        response.status_code = 401
        response._content = b'{"error":"signature_mismatch"}'
        self.client.register_clip_metadados.side_effect = requests.HTTPError(response=response)
        self.deliver()
        self.assertEqual(State.FAILED, self.repo.get("job").state)
        self.assertTrue(self.repo.artifact(self.job, self.job.artifact_location).exists())


if __name__ == "__main__":
    unittest.main()
