"""Regression tests for durable delivery state, retries, and leases."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.application.delivery import RetryPolicy
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState
from src.domain.exceptions import InvalidStateTransition, InvariantViolation
from src.infrastructure.filesystem import (
    FilesystemClipJobRepository,
    FilesystemJobLeaseRepository,
    LeaseUnavailableError,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def make_job(
    *,
    state: ClipJobState = ClipJobState.PROCESSING,
    attempts: int = 0,
) -> ClipJob:
    return ClipJob(
        job_id="clip-1",
        camera_id=CameraId("camera-1"),
        source_location="/queue/clip-1.mp4",
        created_at=NOW,
        state=state,
        attempts=attempts,
    )


class RetryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RetryPolicy(3, timedelta(seconds=10), timedelta(seconds=15))

    def test_failure_increments_attempts_once_and_schedules_exponential_retry(self) -> None:
        first = self.policy.record_failure(make_job(), now=NOW)
        self.assertEqual(1, first.attempts)
        self.assertEqual(ClipJobState.RETRY_PENDING, first.state)
        self.assertEqual(NOW + timedelta(seconds=10), first.next_attempt_at)

        processing = first.begin_retry()
        second = self.policy.record_failure(processing, now=NOW)
        self.assertEqual(2, second.attempts)
        self.assertEqual(NOW + timedelta(seconds=15), second.next_attempt_at)

    def test_last_attempt_and_non_retryable_failure_are_terminal(self) -> None:
        exhausted = self.policy.record_failure(make_job(attempts=2), now=NOW)
        non_retryable = self.policy.record_failure(make_job(), now=NOW, retryable=False)
        self.assertEqual(
            (ClipJobState.FAILED, 3, None),
            (
                exhausted.state,
                exhausted.attempts,
                exhausted.next_attempt_at,
            ),
        )
        self.assertEqual(
            (ClipJobState.FAILED, 1),
            (
                non_retryable.state,
                non_retryable.attempts,
            ),
        )

    def test_retry_is_due_only_at_or_after_next_attempt(self) -> None:
        pending = self.policy.record_failure(make_job(), now=NOW)
        self.assertFalse(self.policy.is_due(pending, now=NOW + timedelta(seconds=9)))
        self.assertTrue(self.policy.is_due(pending, now=NOW + timedelta(seconds=10)))

    def test_begin_retry_does_not_increment_attempts(self) -> None:
        pending = self.policy.record_failure(make_job(), now=NOW)
        resumed = pending.begin_retry()
        self.assertEqual(pending.attempts, resumed.attempts)
        self.assertIsNone(resumed.next_attempt_at)
        with self.assertRaises(InvalidStateTransition):
            make_job().begin_retry()

    def test_policy_rejects_invalid_limits(self) -> None:
        invalid = (
            (0, timedelta(seconds=1), timedelta(seconds=1)),
            (1, timedelta(0), timedelta(seconds=1)),
            (1, timedelta(seconds=2), timedelta(seconds=1)),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(InvariantViolation):
                RetryPolicy(*values)

    def test_job_rejects_invalid_failure_results(self) -> None:
        job = make_job()
        with self.assertRaises(InvariantViolation):
            job.record_failure(
                next_state=ClipJobState.WATERMARKED,
                next_attempt_at=None,
            )
        with self.assertRaises(InvariantViolation):
            job.record_failure(
                next_state=ClipJobState.RETRY_PENDING,
                next_attempt_at=None,
            )
        with self.assertRaises(InvariantViolation):
            job.record_failure(
                next_state=ClipJobState.FAILED,
                next_attempt_at=NOW,
            )


class FilesystemClipJobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = FilesystemClipJobRepository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_writes_v2_and_preserves_legacy_fields(self) -> None:
        path = self.root / "clip-1.json"
        path.write_text(
            json.dumps(
                {
                    "type": "highlight_raw",
                    "file_name": "clip-1.mp4",
                    "meta": {"codec": "h264"},
                    "remote_upload": {"status": "failed"},
                    "status": "queued",
                }
            ),
            encoding="utf-8",
        )

        self.repository.save(make_job(state=ClipJobState.RETRY_PENDING, attempts=1))
        payload = json.loads(path.read_text(encoding="utf-8"))
        restored = self.repository.get("clip-1")

        self.assertEqual(2, payload["schema_version"])
        self.assertEqual("queued_retry", payload["status"])
        self.assertEqual({"codec": "h264"}, payload["meta"])
        self.assertEqual({"status": "failed"}, payload["remote_upload"])
        self.assertEqual(make_job(state=ClipJobState.RETRY_PENDING, attempts=1), restored)
        self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_reads_legacy_v1_without_rewriting_it(self) -> None:
        path = self.root / "legacy.json"
        path.write_text(
            json.dumps(
                {
                    "created_at": "2026-07-27T12:00:00Z",
                    "file_name": "legacy.mp4",
                    "cameraId": "cam-old",
                    "status": "upload_pending",
                    "attempts": 2,
                }
            ),
            encoding="utf-8",
        )
        job = self.repository.get("legacy")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(ClipJobState.RETRY_PENDING, job.state)
        self.assertEqual(CameraId("cam-old"), job.camera_id)
        self.assertNotIn("schema_version", json.loads(path.read_text(encoding="utf-8")))

    def test_corrupt_sidecar_is_quarantined_and_skipped(self) -> None:
        path = self.root / "broken.json"
        path.write_text("{truncated", encoding="utf-8")
        self.assertIsNone(self.repository.get("broken"))
        self.assertFalse(path.exists())
        self.assertEqual(1, len(list(self.root.glob("broken.json.corrupt-*"))))

    def test_list_filters_states_and_quarantines_invalid_entries(self) -> None:
        self.repository.save(make_job(state=ClipJobState.PROCESSING))
        other = ClipJob(
            "clip-2",
            CameraId("camera-2"),
            "/queue/clip-2.mp4",
            NOW,
            ClipJobState.FINALIZED,
        )
        self.repository.save(other)
        (self.root / "invalid.json").write_text("[]", encoding="utf-8")
        jobs = self.repository.list_by_state([ClipJobState.PROCESSING])
        self.assertEqual(["clip-1"], [job.job_id for job in jobs])
        self.assertEqual(1, len(list(self.root.glob("invalid.json.corrupt-*"))))

    def test_missing_root_and_unsafe_identifiers_are_handled(self) -> None:
        missing = FilesystemClipJobRepository(self.root / "missing")
        self.assertEqual((), missing.list_by_state([ClipJobState.QUEUED]))
        self.assertIsNone(missing.get("absent"))
        with self.assertRaises(ValueError):
            self.repository.get("../escape")


class FilesystemJobLeaseRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = FilesystemJobLeaseRepository(
            self.root,
            boot_id="boot-a",
            pid=123,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lease_contains_ownership_and_is_released(self) -> None:
        path = self.root / "clip-1.lease"
        with self.repository.acquire("clip-1", "worker-a", timedelta(seconds=30)):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("worker-a", payload["owner_id"])
            self.assertEqual("boot-a", payload["boot_id"])
            self.assertEqual(123, payload["pid"])
            self.assertIn("acquired_at", payload)
            self.assertEqual(30, payload["ttl_seconds"])
        self.assertFalse(path.exists())

    def test_active_lease_rejects_second_owner(self) -> None:
        with (
            self.repository.acquire("clip-1", "worker-a", timedelta(seconds=30)),
            self.assertRaises(LeaseUnavailableError),
            self.repository.acquire("clip-1", "worker-b", timedelta(seconds=30)),
        ):
            self.fail("an active lease must not be entered")

    def test_expired_and_corrupt_leases_are_recovered(self) -> None:
        path = self.root / "clip-1.lease"
        path.write_text(
            json.dumps(
                {
                    "acquired_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
                    "ttl_seconds": 1,
                }
            ),
            encoding="utf-8",
        )
        with self.repository.acquire("clip-1", "worker-b", timedelta(seconds=5)):
            self.assertEqual(
                "worker-b",
                json.loads(path.read_text(encoding="utf-8"))["owner_id"],
            )

        path.write_text("{bad", encoding="utf-8")
        with self.repository.acquire("clip-1", "worker-c", timedelta(seconds=5)):
            self.assertTrue(path.exists())

    def test_lease_validates_identity_ttl_and_job_id(self) -> None:
        with self.assertRaises(ValueError):
            FilesystemJobLeaseRepository(self.root, boot_id=" ")
        with (
            self.assertRaises(ValueError),
            self.repository.acquire("clip-1", "worker", timedelta(0)),
        ):
            pass
        with (
            self.assertRaises(ValueError),
            self.repository.acquire("clip-1", " ", timedelta(seconds=1)),
        ):
            pass
        with (
            self.assertRaises(ValueError),
            self.repository.acquire("../clip-1", "worker", timedelta(seconds=1)),
        ):
            pass
