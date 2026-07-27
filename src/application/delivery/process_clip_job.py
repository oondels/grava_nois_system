"""Idempotent orchestration of one durable clip-delivery job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from src.application.delivery.retry_policy import RetryPolicy
from src.application.dto import ClipJobSnapshot, RemoteClipRegistration
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
                self.artifacts.cleanup(job, self._artifact(job))
                return self._snapshot(job)
            if job.state in _TERMINAL_STATES:
                return self._snapshot(job)
            if job.state is ClipJobState.FAILED:
                return self._discard(job)
            if job.state is ClipJobState.RETRY_PENDING:
                if not self.retry_policy.is_due(job, now=self.clock.now()):
                    return self._snapshot(job)
                job = job.begin_retry()
                self.jobs.save(job)

            try:
                return self._advance(job)
            except DeliveryStepError as error:
                return self._handle_failure(job, retryable=error.retryable)
            except Exception:
                return self._handle_failure(job, retryable=True)

    def _advance(self, job: ClipJob) -> ClipJobSnapshot:
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
                upload_url=registration.upload_url,
                upload_headers=registration.headers,
            )
            self.jobs.save(job)

        if job.state is ClipJobState.REGISTERED:
            registration = self._registration(job)
            self.backend.upload(registration, self._artifact(job))
            job = job.transition_to(ClipJobState.UPLOADED)
            self.jobs.save(job)

        if job.state is ClipJobState.UPLOADED:
            if job.remote_clip_id is None:
                raise DeliveryStepError("uploaded job has no remote clip id", retryable=False)
            self.backend.finalize(job.remote_clip_id)
            job = job.transition_to(ClipJobState.FINALIZED)
            self.jobs.save(job)
            self.artifacts.cleanup(job, self._artifact(job))

        return self._snapshot(job)

    def _handle_failure(self, job: ClipJob, *, retryable: bool) -> ClipJobSnapshot:
        current = self.jobs.get(job.job_id) or job
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
    def _registration(job: ClipJob) -> RemoteClipRegistration:
        if job.remote_clip_id is None or job.upload_url is None:
            raise DeliveryStepError("registered job has incomplete registration", retryable=False)
        return RemoteClipRegistration(
            clip_id=job.remote_clip_id,
            upload_url=job.upload_url,
            headers=job.upload_headers,
        )

    @classmethod
    def _snapshot(cls, job: ClipJob) -> ClipJobSnapshot:
        return ClipJobSnapshot(
            job_id=job.job_id,
            state=job.state,
            attempts=job.attempts,
            artifact_location=cls._artifact(job),
        )
