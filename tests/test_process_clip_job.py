"""Use-case tests for the restart-safe delivery pipeline."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.application.delivery import ProcessClipJob, RetryPolicy
from src.application.dto import RemoteClipRegistration, UploadReceipt
from src.application.exceptions import DeliveryStepError, NotFoundError
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState
from src.infrastructure.filesystem import LegacyArtifactStore
from src.infrastructure.http import LegacyVideoBackendGateway

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class MemoryJobs:
    def __init__(self, job: ClipJob | None) -> None:
        self.job = job
        self.saved: list[ClipJobState] = []

    def get(self, job_id: str) -> ClipJob | None:
        return self.job if self.job is not None and self.job.job_id == job_id else None

    def save(self, job: ClipJob) -> None:
        self.job = job
        self.saved.append(job.state)

    def list_by_state(self, states: object) -> tuple[()]:
        return ()


class CrashAfterCheckpointJobs(MemoryJobs):
    def __init__(self, job: ClipJob, crash_state: ClipJobState) -> None:
        super().__init__(job)
        self.crash_state = crash_state
        self.crashed = False

    def save(self, job: ClipJob) -> None:
        super().save(job)
        if job.state is self.crash_state and not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt("simulated process death")


class FakeLeases:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, timedelta]] = []

    def acquire(self, job_id: str, owner_id: str, ttl: timedelta) -> nullcontext[None]:
        self.calls.append((job_id, owner_id, ttl))
        return nullcontext()


class FakeMedia:
    def __init__(self) -> None:
        self.watermarks = 0
        self.probes = 0

    def apply_watermark(self, source: str, output: str) -> None:
        self.watermarks += 1

    def probe(self, source: str) -> dict[str, object]:
        self.probes += 1
        return {"codec": "h264"}

    def concatenate(self, inputs: object, output: str) -> None:
        raise AssertionError("not used by delivery")


class FakeBackend:
    def __init__(self) -> None:
        self.registers = 0
        self.uploads = 0
        self.finalizes = 0
        self.failure: tuple[str, bool] | None = None

    def _fail(self, step: str) -> None:
        if self.failure is not None and self.failure[0] == step:
            raise DeliveryStepError("dependency failed", retryable=self.failure[1])

    def register(self, job: ClipJob, metadata: dict[str, object]) -> RemoteClipRegistration:
        self.registers += 1
        self._fail("register")
        return RemoteClipRegistration("remote-1", "https://upload", {"x": "1"})

    def upload(self, registration: RemoteClipRegistration, artifact_location: str) -> UploadReceipt:
        self.uploads += 1
        self._fail("upload")
        return UploadReceipt(200, {"etag": "abc"})

    def finalize(self, remote_clip_id: str) -> None:
        self.finalizes += 1
        self._fail("finalize")


class FakeArtifacts:
    def __init__(self) -> None:
        self.preserved = 0
        self.discarded = 0
        self.cleaned = 0

    def watermarked_location(self, job: ClipJob) -> str:
        return f"/watermarked/{job.job_id}.mp4"

    def preserve(self, job: ClipJob, artifact_location: str) -> None:
        self.preserved += 1

    def discard(self, job: ClipJob, artifact_location: str) -> None:
        self.discarded += 1

    def cleanup(self, job: ClipJob, artifact_location: str) -> None:
        self.cleaned += 1


def make_job(state: ClipJobState = ClipJobState.QUEUED) -> ClipJob:
    base = ClipJob("job-1", CameraId("cam-1"), "/queue/job-1.mp4", NOW)
    if state is ClipJobState.QUEUED:
        return base
    processing = base.transition_to(ClipJobState.PROCESSING)
    if state is ClipJobState.PROCESSING:
        return processing
    watermarked = processing.with_artifact("/watermarked/job-1.mp4")
    if state is ClipJobState.WATERMARKED:
        return watermarked
    registered = watermarked.with_registration(
        remote_clip_id="remote-1",
        upload_url="https://upload",
        upload_headers={"x": "1"},
    )
    if state is ClipJobState.REGISTERED:
        return registered
    uploaded = registered.transition_to(ClipJobState.UPLOADED)
    if state is ClipJobState.UPLOADED:
        return uploaded
    return uploaded.transition_to(state)


class ProcessClipJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = MemoryJobs(make_job())
        self.leases = FakeLeases()
        self.media = FakeMedia()
        self.backend = FakeBackend()
        self.artifacts = FakeArtifacts()
        self.clock = FakeClock()

    def use_case(self, *, dev_mode: bool = False) -> ProcessClipJob:
        return ProcessClipJob(
            jobs=self.jobs,
            leases=self.leases,
            media=self.media,
            backend=self.backend,
            artifacts=self.artifacts,
            retry_policy=RetryPolicy(
                max_attempts=3,
                base_delay=timedelta(seconds=10),
                max_delay=timedelta(seconds=30),
            ),
            clock=self.clock,
            owner_id="worker-1",
            lease_ttl=timedelta(seconds=60),
            dev_mode=dev_mode,
        )

    def test_complete_flow_checkpoints_each_external_step(self) -> None:
        result = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.FINALIZED, result.state)
        self.assertEqual(
            [
                ClipJobState.PROCESSING,
                ClipJobState.WATERMARKED,
                ClipJobState.REGISTERED,
                ClipJobState.UPLOADED,
                ClipJobState.FINALIZED,
            ],
            self.jobs.saved,
        )
        self.assertEqual(
            (1, 1, 1),
            (
                self.backend.registers,
                self.backend.uploads,
                self.backend.finalizes,
            ),
        )
        self.assertEqual(1, self.artifacts.cleaned)
        self.assertEqual(
            [("job-1", "worker-1", timedelta(seconds=60))],
            self.leases.calls,
        )

    def test_restart_resumes_without_repeating_completed_steps(self) -> None:
        expected = {
            ClipJobState.WATERMARKED: (0, 1, 1, 1),
            ClipJobState.REGISTERED: (0, 0, 1, 1),
            ClipJobState.UPLOADED: (0, 0, 0, 1),
            ClipJobState.FINALIZED: (0, 0, 0, 0),
        }
        for state, counts in expected.items():
            with self.subTest(state=state):
                self.setUp()
                self.jobs.job = make_job(state)
                result = self.use_case().execute("job-1")
                self.assertEqual(ClipJobState.FINALIZED, result.state)
                self.assertEqual(
                    counts,
                    (
                        self.media.watermarks,
                        self.backend.registers,
                        self.backend.uploads,
                        self.backend.finalizes,
                    ),
                )

    def test_retryable_failure_is_scheduled_once_and_resumes_checkpoint(self) -> None:
        self.backend.failure = ("upload", True)
        first = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.RETRY_PENDING, first.state)
        self.assertEqual(1, first.attempts)
        assert self.jobs.job is not None
        self.assertEqual(ClipJobState.REGISTERED, self.jobs.job.retry_from)

        not_due = self.use_case().execute("job-1")
        self.assertEqual(1, not_due.attempts)
        self.assertEqual(1, self.backend.uploads)

        self.clock.value += timedelta(seconds=10)
        self.backend.failure = None
        completed = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.FINALIZED, completed.state)
        self.assertEqual(1, self.backend.registers)
        self.assertEqual(2, self.backend.uploads)

    def test_terminal_failure_discards_and_does_not_retry(self) -> None:
        self.backend.failure = ("register", False)
        result = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.DISCARDED, result.state)
        self.assertEqual(1, result.attempts)
        self.assertEqual(1, self.artifacts.discarded)
        self.assertIn(ClipJobState.FAILED, self.jobs.saved)

    def test_finalize_retry_does_not_repeat_upload(self) -> None:
        self.jobs.job = make_job(ClipJobState.UPLOADED)
        self.backend.failure = ("finalize", True)
        first = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.RETRY_PENDING, first.state)
        self.assertEqual(0, self.backend.uploads)

        self.clock.value += timedelta(seconds=10)
        self.backend.failure = None
        completed = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.FINALIZED, completed.state)
        self.assertEqual(0, self.backend.uploads)
        self.assertEqual(2, self.backend.finalizes)

    def test_crash_after_uploaded_checkpoint_resumes_at_finalize_without_reupload(self) -> None:
        self.jobs = CrashAfterCheckpointJobs(
            make_job(ClipJobState.REGISTERED),
            ClipJobState.UPLOADED,
        )

        with self.assertRaisesRegex(KeyboardInterrupt, "simulated process death"):
            self.use_case().execute("job-1")

        self.assertEqual(ClipJobState.UPLOADED, self.jobs.job.state)
        self.assertEqual(1, self.backend.uploads)
        completed = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.FINALIZED, completed.state)
        self.assertEqual(1, self.backend.uploads)
        self.assertEqual(1, self.backend.finalizes)

    def test_terminal_discard_is_idempotent_on_later_execution(self) -> None:
        self.backend.failure = ("register", False)
        first = self.use_case().execute("job-1")
        second = self.use_case().execute("job-1")

        self.assertEqual(ClipJobState.DISCARDED, first.state)
        self.assertEqual(ClipJobState.DISCARDED, second.state)
        self.assertEqual(1, self.artifacts.discarded)
        self.assertEqual(1, self.backend.registers)

    def test_unknown_failure_defaults_to_retryable(self) -> None:
        self.media.apply_watermark = lambda source, output: (_ for _ in ()).throw(
            RuntimeError("ffmpeg")
        )
        result = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.RETRY_PENDING, result.state)
        self.assertEqual(1, result.attempts)

    def test_dev_mode_preserves_without_remote_calls(self) -> None:
        result = self.use_case(dev_mode=True).execute("job-1")
        self.assertEqual(ClipJobState.DEV_PRESERVED, result.state)
        self.assertEqual(1, self.artifacts.preserved)
        self.assertEqual(
            (0, 0, 0),
            (
                self.backend.registers,
                self.backend.uploads,
                self.backend.finalizes,
            ),
        )

    def test_missing_job_raises_not_found(self) -> None:
        self.jobs.job = None
        with self.assertRaises(NotFoundError):
            self.use_case().execute("missing")

    def test_preexisting_failed_job_is_discarded(self) -> None:
        self.jobs.job = replace(
            make_job(ClipJobState.PROCESSING),
            state=ClipJobState.FAILED,
            attempts=3,
        )
        result = self.use_case().execute("job-1")
        self.assertEqual(ClipJobState.DISCARDED, result.state)
        self.assertEqual(1, self.artifacts.discarded)


class LegacyCallableAdapterTests(unittest.TestCase):
    def test_backend_gateway_delegates_without_reading_global_configuration(self) -> None:
        calls: list[str] = []
        registration = RemoteClipRegistration("remote", "https://upload", {})
        receipt = UploadReceipt(200, {})
        gateway = LegacyVideoBackendGateway(
            register=lambda job, metadata: calls.append("register") or registration,
            upload=lambda remote, artifact: calls.append("upload") or receipt,
            finalize=lambda clip_id: calls.append("finalize"),
        )

        self.assertIs(registration, gateway.register(make_job(), {}))
        self.assertIs(receipt, gateway.upload(registration, "/artifact.mp4"))
        gateway.finalize("remote")
        self.assertEqual(["register", "upload", "finalize"], calls)

    def test_artifact_store_delegates_all_lifecycle_operations(self) -> None:
        calls: list[str] = []
        store = LegacyArtifactStore(
            watermarked_location=lambda job: "/watermarked.mp4",
            preserve=lambda job, path: calls.append("preserve"),
            discard=lambda job, path: calls.append("discard"),
            cleanup=lambda job, path: calls.append("cleanup"),
        )
        job = make_job()
        self.assertEqual("/watermarked.mp4", store.watermarked_location(job))
        store.preserve(job, "/watermarked.mp4")
        store.discard(job, "/watermarked.mp4")
        store.cleanup(job, "/watermarked.mp4")
        self.assertEqual(["preserve", "discard", "cleanup"], calls)
