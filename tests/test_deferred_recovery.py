"""Crash, policy, migration and acknowledgment contracts, using isolated storage."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.application.delivery.deferred_coordinator import DeferredCoordinator
from src.bootstrap.deferred_runtime import DeferredArtifacts, DeferredRuntime
from src.config.config_loader import OperationalConfig
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob
from src.domain.delivery import ClipJobState as State
from src.domain.replay.processing_schedule import DeviceActivity
from src.infrastructure.filesystem.deferred_repository import (
    DeferredJobRepository,
    DeferredLeases,
    LockBusy,
    exclusive_file,
    require_persistent_storage,
)
from src.services.mqtt.operational_event_service import OperationalEventService, sign_operational

UTC = UTC
IDENTITY = {"device_id": "edge", "client_id": "client", "venue_id": "venue"}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = DeferredJobRepository(self.root / "jobs")
        self.now = datetime(2026, 9, 21, 12, tzinfo=UTC)
        self.mono = [0.0]
        self.activity = DeviceActivity(lambda: self.mono[0])
        self.mono[0] = 1800
        self.media = Mock()
        self.media.concatenate.return_value = {"duration_sec": 2}
        self.media.watermark.return_value = {"duration_sec": 2}
        self.config = OperationalConfig()
        self.config.operation_window.time_zone = "UTC"
        self.storage = Mock()
        self.coordinator = DeferredCoordinator(
            self.repo,
            self.media,
            Mock(),
            Mock(),
            DeferredLeases(self.repo),
            lambda: exclusive_file(self.root / "heavy.lock"),
            self.activity,
            self.storage,
            Mock(),
            lambda: self.config,
            SimpleNamespace(pending={}),
            SimpleNamespace(now=lambda: self.now),
        )
        for name in ("a", "b"):
            self.repo.save(
                ClipJob(
                    name,
                    CameraId(name),
                    "artifacts/assembled.mp4",
                    self.now,
                    schema_version=3,
                    details={
                        "captured_at": self.now.isoformat(),
                        "source_kind": "segments",
                        "segments": [{"size_bytes": 10, "path": "segments/part.ts"}],
                        "policy": {"max_attempts": 3, "schedule_required": True},
                    },
                )
            )

    def test_idle_drains_without_another_thirty_minutes(self):
        self.coordinator.process_media_once()
        self.coordinator.process_media_once()
        self.assertTrue(all(j.details["thumbnail_complete"] for j in self.repo.all()))
        self.assertEqual(2, self.media.concatenate.call_count)

    def test_new_valid_click_finishes_current_and_stops_next(self):
        def concat(*args):
            self.activity.record()  # independent of whether preservation succeeds
            return {"duration_sec": 2}

        self.media.concatenate.side_effect = concat
        self.coordinator.process_media_once()
        self.coordinator.process_media_once()
        self.assertEqual(State.WATERMARKED, self.repo.get("a").state)
        self.assertEqual(State.QUEUED, self.repo.get("b").state)

    def test_click_does_not_cancel_explicit_window(self):
        self.now = self.now.replace(hour=4)
        self.media.concatenate.side_effect = lambda *args: (
            self.activity.record() or {"duration_sec": 2}
        )
        self.coordinator.process_media_once()
        self.coordinator.process_media_once()
        self.assertEqual(2, self.media.concatenate.call_count)

    def test_five_oclock_finishes_current_but_gates_next(self):
        self.now = self.now.replace(hour=4, minute=59)
        self.activity.record()

        def concat(*args):
            self.now = self.now.replace(hour=5, minute=0)
            return {"duration_sec": 2}

        self.media.concatenate.side_effect = concat
        self.coordinator.process_media_once()
        self.coordinator.process_media_once()
        self.assertEqual(State.WATERMARKED, self.repo.get("a").state)
        self.assertEqual(State.QUEUED, self.repo.get("b").state)

    def test_rechecks_after_lock_before_starting(self):
        @contextmanager
        def lock():
            self.activity.record()
            yield 0

        self.coordinator.heavy_lock = lock
        self.coordinator.process_media_once()
        self.media.concatenate.assert_not_called()

    def test_backoff_on_first_job_does_not_block_second(self):
        self.media.concatenate.side_effect = [RuntimeError("failed"), {"duration_sec": 2}]
        self.coordinator.process_media_once()
        self.coordinator.process_media_once()
        self.assertEqual(State.RETRY_PENDING, self.repo.get("a").state)
        self.assertEqual(State.WATERMARKED, self.repo.get("b").state)

    def test_corrupted_policy_does_not_block_other_jobs(self):
        job = self.repo.get("a")
        self.repo.save(replace(job, details={**job.details, "policy": "corrupt"}))
        self.coordinator.process_media_once()
        self.assertEqual(1, self.repo.invalid_count())
        self.assertEqual(State.WATERMARKED, self.repo.get("b").state)

    def test_restart_recovers_last_media_checkpoint(self):
        self.repo.save(
            replace(
                self.repo.get("a"),
                state=State.PROCESSING,
                details={**self.repo.get("a").details, "media_checkpoint": "ASSEMBLED"},
            )
        )
        self.repo.recover()
        self.assertEqual(State.ASSEMBLED, self.repo.get("a").state)


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runtime = DeferredRuntime.__new__(DeferredRuntime)
        self.runtime.base = self.base
        self.runtime.root = self.base / "queue_raw" / ".deferred"
        self.runtime.jobs = DeferredJobRepository(self.runtime.root)
        self.runtime.legacy_roots = [
            self.base / "queue_raw",
            self.base / "failed_clips",
            self.base / "highlights_wm",
        ]
        self.runtime.identity = IDENTITY
        self.runtime.cameras = []
        self.runtime.storage = Mock()
        self.runtime.events = Mock()
        self.runtime.policy = lambda camera_id=None: {"max_attempts": 3, "assets_saved": True}
        self.config = OperationalConfig()
        self.config.processing.deferred_enabled = True
        patcher = patch(
            "src.bootstrap.deferred_runtime.get_effective_config", return_value=self.config
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.base / "queue_raw" / "cam" / "old.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"assembled")
        self.meta = {
            "created_at": "2026-09-20T12:00:00+00:00",
            "camera_id": "cam",
            "status": "queued",
        }

    def migrate(self):
        self.source.with_suffix(".json").write_text(json.dumps(self.meta))
        self.runtime._import_legacy()
        return self.runtime.jobs.all()

    def test_legacy_camera_is_recovered_from_existing_highlight_filename(self):
        source = Path("highlight_cam_quadra1_20260920-120000-123456Z.mp4")
        self.assertEqual("cam_quadra1", self.runtime._legacy_camera_id({}, source))
        self.assertEqual("legacy_unknown", self.runtime._legacy_camera_id({}, Path("old.mp4")))

    def test_raw_legacy_is_already_assembled_and_import_is_idempotent(self):
        jobs = self.migrate()
        self.assertEqual(1, len(jobs))
        self.assertEqual(State.ASSEMBLED, jobs[0].state)
        self.assertEqual("assembled_video", jobs[0].details["source_kind"])
        self.assertTrue(self.source.exists())
        self.assertEqual(jobs[0].job_id, self.migrate()[0].job_id)

    def test_ready_legacy_is_reused_and_originals_removed_only_at_cleanup(self):
        final = self.base / "highlights_wm" / self.source.name
        final.parent.mkdir()
        final.write_bytes(b"encoded")
        self.meta.update(meta_wm={"duration_sec": 5}, wm_path=str(final))
        job = self.migrate()[0]
        self.assertEqual(State.WATERMARKED, job.state)
        self.assertTrue(final.exists())
        store = DeferredArtifacts(self.runtime.jobs, self.runtime.legacy_roots)
        finalized = replace(job, state=State.FINALIZED)
        self.runtime.jobs.save(finalized)
        store.cleanup(finalized, job.artifact_location)
        self.assertFalse(self.source.exists())
        self.assertFalse(final.exists())
        self.assertTrue(self.source.with_suffix(".deferred.json").exists())
        self.assertTrue(self.runtime.jobs.get(job.job_id).details["cleaned"])

    def test_missing_final_is_not_mistaken_for_raw_or_reencoded(self):
        self.meta.update(meta_wm={"duration_sec": 5})
        self.assertEqual((), self.migrate())
        self.assertTrue(self.source.exists())
        self.assertEqual(1, self.runtime.legacy_pending)

    def test_v2_uploaded_preserves_receipt_and_remote_identity(self):
        self.meta.update(
            schema_version=2,
            state="UPLOADED",
            artifact_location=str(self.source),
            remote_clip_id="remote",
            upload_sha256=hashlib.sha256(b"assembled").hexdigest(),
            upload_etag="etag",
        )
        job = self.migrate()[0]
        self.assertEqual(State.UPLOADED, job.state)
        self.assertEqual("remote", job.remote_clip_id)
        self.assertEqual("etag", job.upload_etag)
        self.assertTrue(job.details["thumbnail_complete"])

    def test_unknown_format_and_changed_uploaded_bytes_are_retained(self):
        self.meta.update(schema_version=99)
        self.assertEqual((), self.migrate())
        self.meta.update(
            schema_version=2,
            state="UPLOADED",
            artifact_location=str(self.source),
            remote_clip_id="remote",
            upload_sha256="0" * 64,
        )
        self.assertEqual((), self.migrate())
        self.assertTrue(self.source.exists())

    def test_disabling_flag_preserves_scheduled_v3_and_legacy_content(self):
        old = self.migrate()[0]
        self.config.processing.deferred_enabled = False
        self.assertTrue(self.migrate()[0].details["policy"]["schedule_required"])
        self.assertEqual(old.job_id, self.runtime.jobs.all()[0].job_id)


class RuntimeWiringTests(unittest.TestCase):
    def test_real_runtime_starts_independent_workers_and_excludes_duplicate_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = OperationalConfig()
            config.processing.deferred_enabled = True
            client = Mock()
            with patch("src.bootstrap.deferred_runtime.get_effective_config", return_value=config):
                args = dict(
                    base=Path(tmp),
                    cameras=[],
                    mqtt_client=client,
                    topic_for=lambda suffix: "grn/devices/edge/" + suffix,
                    identity=IDENTITY,
                    secret="fixture",
                    watermark=Path(tmp) / "logo.png",
                    client_watermark=None,
                    top_watermark=None,
                    dev_mode=True,
                )
                runtime = DeferredRuntime(**args)
                try:
                    with self.assertRaises(LockBusy):
                        DeferredRuntime(**args)
                    imports = []
                    original_import = runtime._import_legacy

                    def record_import():
                        imports.append(threading.current_thread().name)
                        original_import()

                    runtime._import_legacy = record_import
                    runtime.start()
                    self.assertNotIn(threading.current_thread().name, imports)
                    runtime._monitor()
                    self.assertEqual(4, len(runtime._threads))
                    self.assertEqual({}, runtime.snapshot()["queues"])
                    self.assertTrue(runtime.policy()["dev"])
                    self.assertTrue((runtime.events.root / "state.json").exists())
                    self.assertEqual(2, client.subscribe.call_count)
                finally:
                    runtime.stop()
                self.assertFalse(any(t.is_alive() for t in runtime._threads))
                with exclusive_file(runtime.root / "runtime.lock"):
                    pass


class PersistenceSafetyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("timeout") and shutil.which("nice"), "coreutils unavailable")
    def test_orphan_media_keeps_global_lock_until_child_exits(self):
        script = """
import sys
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob
from src.infrastructure.filesystem.deferred_repository import DeferredJobRepository, exclusive_file
from src.infrastructure.media.deferred_media import DeferredMedia
root = Path(sys.argv[1])
jobs = DeferredJobRepository(root / 'jobs')
job = ClipJob('test', CameraId('camera'), 'artifacts/a.mp4', datetime.now(timezone.utc))
child = ('import sys,time; from pathlib import Path; '
         'Path(sys.argv[1]).touch(); time.sleep(2)')
command = [sys.executable, '-c', child, str(root / 'ready')]
with exclusive_file(root / 'heavy.lock') as fd:
    DeferredMedia(jobs, Mock()).run(command, job=job, lock_fd=fd)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = subprocess.Popen(
                [sys.executable, "-c", script, tmp],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while not (root / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / "ready").exists())
                process.kill()
                process.wait(timeout=2)
                with self.assertRaises(LockBusy), exclusive_file(root / "heavy.lock"):
                    pass
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with exclusive_file(root / "heavy.lock"):
                            break
                    except LockBusy:
                        time.sleep(0.02)
                else:
                    self.fail("orphan media did not release the device lock")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

    def test_tmpfs_is_rejected_even_when_not_named_shm(self):
        with patch.object(
            Path,
            "read_text",
            return_value=(
                "1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / /ram rw - tmpfs tmpfs rw\n"
            ),
        ):
            require_persistent_storage(Path("/disk/jobs"))
            with self.assertRaisesRegex(ValueError, "persistent"):
                require_persistent_storage(Path("/ram/jobs"))

    def test_late_snapshot_ack_cannot_clear_current_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = OperationalEventService(tmp, Mock(), lambda t: t, IDENTITY, "test-secret")
            service.snapshot({"running": "old"})
            old = json.loads((Path(tmp) / "state.json").read_text())
            service.snapshot({"running": None})
            ack = {
                **IDENTITY,
                "event_id": "state",
                "ack_hash": old["content_hash"],
                "status": "persisted",
            }
            ack["signature"] = sign_operational(ack, "test-secret")
            service._ack("state/ack", json.dumps(ack).encode())
            self.assertIsNone(
                json.loads((Path(tmp) / "state.json").read_text())["operational"]["running"]
            )


if __name__ == "__main__":
    unittest.main()
