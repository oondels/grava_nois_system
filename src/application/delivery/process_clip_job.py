"""Idempotent orchestration of one durable clip-delivery job."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from src.application.delivery.retry_policy import RetryPolicy
from src.application.dto import ClipJobSnapshot, RemoteClipRegistration, UploadReceipt
from src.application.exceptions import DeliveryStepError, NotFoundError
from src.application.ports import (
    ArtifactStore,
    ClipJobRepository,
    Clock,
    JobLeaseRepository,
    MediaTool,
    VideoBackendGateway,
)
from src.domain.delivery import ClipJob, ClipJobState

_TERMINAL_STATES = {
    ClipJobState.DISCARDED,
    ClipJobState.DEV_PRESERVED,
}


@dataclass(frozen=True, slots=True)
class ProcessClipJob:
    jobs: ClipJobRepository
    leases: JobLeaseRepository
    media: MediaTool
    backend: VideoBackendGateway
    artifacts: ArtifactStore
    retry_policy: RetryPolicy
    clock: Clock
    owner_id: str
    lease_ttl: timedelta
    dev_mode: bool = False

    def execute(self, job_id: str) -> ClipJobSnapshot:
        with self.leases.acquire(job_id, self.owner_id, self.lease_ttl):
            job = self.jobs.get(job_id)
            if job is None:
                raise NotFoundError(f"clip job not found: {job_id}")

            if job.state is ClipJobState.FINALIZED:
                try:
                    self.artifacts.cleanup(job, self._artifact(job))
                except Exception:
                    if job.schema_version != 3:
                        raise
                    return self._handle_failure(job, retryable=True)
                return self._snapshot(job)
            if job.state in _TERMINAL_STATES:
                return self._snapshot(job)
            if job.state is ClipJobState.FAILED:
                return self._snapshot(job) if job.schema_version == 3 else self._discard(job)
            if job.state is ClipJobState.RETRY_PENDING:
                if not self.retry_policy.is_due(job, now=self.clock.now()):
                    return self._snapshot(job)
                job = job.begin_retry()
                self.jobs.save(job)

            try:
                return self._advance(job)
            except DeliveryStepError as error:
                return self._handle_failure(job, retryable=error.retryable, error=error)
            except Exception:
                return self._handle_failure(job, retryable=True)

    def _advance(self, job: ClipJob) -> ClipJobSnapshot:
        registration: RemoteClipRegistration | None = None

        if job.state is ClipJobState.QUEUED:
            job = job.transition_to(ClipJobState.PROCESSING)
            self.jobs.save(job)

        if job.state is ClipJobState.PROCESSING:
            artifact = self.artifacts.watermarked_location(job)
            self.media.apply_watermark(job.source_location, artifact)
            job = job.with_artifact(artifact)
            self.jobs.save(job)

        if self.dev_mode and job.state is ClipJobState.WATERMARKED:
            artifact = self._artifact(job)
            self.artifacts.preserve(job, artifact)
            job = job.transition_to(ClipJobState.DEV_PRESERVED)
            self.jobs.save(job)
            return self._snapshot(job)

        if job.state is ClipJobState.WATERMARKED:
            artifact = self._artifact(job)
            registration = self.backend.register(job, self.media.probe(artifact))
            job = job.with_registration(
                remote_clip_id=registration.clip_id,
            )
            self.jobs.save(job)

        if job.state is ClipJobState.REGISTERED:
            if registration is None:
                registration = self.backend.register(
                    job,
                    self.media.probe(self._artifact(job)),
                )
                if job.remote_clip_id is None:
                    job = job.refresh_registration(registration.clip_id)
                    self.jobs.save(job)
                elif registration.clip_id != job.remote_clip_id:
                    raise DeliveryStepError(
                        "backend returned a different clip id while refreshing registration",
                        retryable=False,
                    )
            receipt = self.backend.upload(registration, self._artifact(job))
            job = job.with_upload_receipt(
                size_bytes=receipt.size_bytes,
                sha256=receipt.sha256,
                etag=receipt.etag,
            )
            self.jobs.save(job)

        if job.state is ClipJobState.UPLOADED:
            if job.remote_clip_id is None:
                raise DeliveryStepError("uploaded job has no remote clip id", retryable=False)
            self.backend.finalize(job.remote_clip_id, self._upload_receipt(job))
            job = job.transition_to(ClipJobState.FINALIZED)
            self.jobs.save(job)
            self.artifacts.cleanup(job, self._artifact(job))

        return self._snapshot(job)

    def _handle_failure(
        self, job: ClipJob, *, retryable: bool, error: Any = None
    ) -> ClipJobSnapshot:
        current = self.jobs.get(job.job_id) or job
        if current.schema_version == 3:
            if current.state is ClipJobState.FINALIZED:
                # Storage confirmation is irreversible. Cleanup cannot turn it
                # into a failed upload or repeat remote effects.
                current = replace(
                    current,
                    next_attempt_at=self.clock.now() + timedelta(seconds=120),
                    details={
                        **current.details,
                        "last_error": {
                            "stage": "cleanup",
                            "code": "cleanup_pending",
                            "occurred_at": self.clock.now().isoformat(),
                        },
                    },
                )
                self.jobs.save(current)
                return self._snapshot(current)
            stage = {
                ClipJobState.WATERMARKED: "registration",
                ClipJobState.REGISTERED: "upload",
                ClipJobState.UPLOADED: "finalize",
            }.get(current.state, "processing")
            attempts = dict(current.details.get("attempts_by_stage", {}))
            code = getattr(error, "code", "delivery_failed")
            blocked = getattr(error, "blocked", False)
            if not blocked:
                attempts[stage] = attempts.get(stage, 0) + 1
            terminal = not blocked and (
                not retryable or attempts[stage] >= self.retry_policy.max_attempts
            )
            state = (
                ClipJobState.BLOCKED
                if blocked
                else (ClipJobState.FAILED if terminal else ClipJobState.RETRY_PENDING)
            )
            failed = replace(
                current,
                state=state,
                attempts=current.attempts + (0 if blocked else 1),
                retry_from=current.state,
                next_attempt_at=None
                if terminal
                else self.clock.now()
                + timedelta(
                    seconds=900 if blocked else min(900, 120 * 2 ** min(attempts[stage] - 1, 3))
                ),
                details={
                    **current.details,
                    "attempts_by_stage": attempts,
                    "last_error": {
                        "stage": stage,
                        "code": code,
                        "occurred_at": self.clock.now().isoformat(),
                    },
                },
            )
            self.jobs.save(failed)
            return self._snapshot(failed)
        failed = self.retry_policy.record_failure(
            current,
            now=self.clock.now(),
            retryable=retryable,
        )
        self.jobs.save(failed)
        if failed.state is ClipJobState.FAILED:
            return self._discard(failed)
        return self._snapshot(failed)

    def _discard(self, job: ClipJob) -> ClipJobSnapshot:
        self.artifacts.discard(job, self._artifact(job))
        discarded = job.transition_to(ClipJobState.DISCARDED)
        self.jobs.save(discarded)
        return self._snapshot(discarded)

    @staticmethod
    def _artifact(job: ClipJob) -> str:
        return job.artifact_location or job.source_location

    @staticmethod
    def _upload_receipt(job: ClipJob) -> UploadReceipt:
        if job.upload_size_bytes is None or job.upload_sha256 is None:
            raise DeliveryStepError("uploaded job has no integrity receipt", retryable=False)
        return UploadReceipt(
            status_code=200,
            response_headers={},
            size_bytes=job.upload_size_bytes,
            sha256=job.upload_sha256,
            etag=job.upload_etag,
        )

    @classmethod
    def _snapshot(cls, job: ClipJob) -> ClipJobSnapshot:
        return ClipJobSnapshot(
            job_id=job.job_id,
            state=job.state,
            attempts=job.attempts,
            artifact_location=cls._artifact(job),
        )
