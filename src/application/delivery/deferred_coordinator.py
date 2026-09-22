"""One device-wide media consumer and independent delivery advancement."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from src.application.delivery.process_clip_job import ProcessClipJob
from src.application.delivery.retry_policy import RetryPolicy
from src.domain.delivery import ClipJob, ClipJobState


class UTCClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class DeferredCoordinator:
    def __init__(
        self,
        jobs: Any,
        media: Any,
        gateway: Any,
        artifacts: Any,
        leases: Any,
        heavy_lock: Any,
        activity: Any,
        storage: Any,
        events: Any,
        config: Any,
        preservation: Any,
        clock: Any = None,
    ) -> None:
        self.jobs, self.media, self.gateway, self.artifacts = jobs, media, gateway, artifacts
        self.leases, self.heavy_lock = leases, heavy_lock
        self.activity, self.storage, self.events = activity, storage, events
        self.config, self.preservation = config, preservation
        self.clock = clock or UTCClock()
        self.running: dict[str, Any] | None = None

    def _due(self, job: ClipJob) -> ClipJob | None:
        if job.state in {ClipJobState.RETRY_PENDING, ClipJobState.BLOCKED}:
            if job.next_attempt_at and job.next_attempt_at > self.clock.now():
                return None
            if job.retry_from is None:
                return None
            return replace(job, state=job.retry_from, retry_from=None, next_attempt_at=None)
        return job

    def process_media_once(self) -> None:
        if self.preservation.pending or not self.storage.memory_safe():
            return
        config = self.config()
        authorization = self.activity.authorization(
            self.clock.now(),
            config.operation_window.time_zone,
            config.processing.additional_windows,
        )
        for saved in self.jobs.all():
            job = self._due(saved)
            if job is None or job.state not in {
                ClipJobState.QUEUED,
                ClipJobState.PROCESSING,
                ClipJobState.ASSEMBLED,
                ClipJobState.WATERMARKED,
            }:
                continue
            if job.state is ClipJobState.WATERMARKED and job.details.get("thumbnail_complete"):
                continue
            if not authorization and job.details.get("policy", {}).get("schedule_required", True):
                continue
            with (
                self.heavy_lock() as lock_fd,
                self.leases.acquire(job.job_id, "media", timedelta(hours=1)),
            ):
                # A click, a config update or the end of a window may have occurred
                # while selecting the oldest job. Authorization gates each clip.
                config = self.config()
                authorization = self.activity.authorization(
                    self.clock.now(),
                    config.operation_window.time_zone,
                    config.processing.additional_windows,
                )
                if self.preservation.pending or (
                    not authorization
                    and job.details.get("policy", {}).get("schedule_required", True)
                ):
                    return
                self.jobs.save(job)
                self.running = {
                    "job_id": job.job_id,
                    "stage": "media",
                    "authorization": authorization,
                }
                stage = "concat"
                try:
                    required = sum(s["size_bytes"] for s in job.details.get("segments", []))
                    if not required:
                        required = (
                            self.jobs.artifact(job, job.artifact_location or job.source_location)
                            .stat()
                            .st_size
                        )
                    self.storage.check(self.jobs.directory(job.job_id), int(required * 3.6))
                    if job.state is ClipJobState.PROCESSING:
                        job = replace(
                            job, state=ClipJobState(job.details.get("media_checkpoint", "QUEUED"))
                        )
                    if job.state is ClipJobState.QUEUED:
                        self.jobs.save(
                            replace(
                                job,
                                state=ClipJobState.PROCESSING,
                                details={**job.details, "media_checkpoint": "QUEUED"},
                            )
                        )
                        if job.details["source_kind"] == "segments":
                            metadata = self.media.concatenate(job, lock_fd)
                        else:
                            metadata = self.media.probe(
                                self.jobs.artifact(job, job.source_location)
                            )
                        job = replace(
                            job,
                            state=ClipJobState.ASSEMBLED,
                            details={**job.details, "duration_sec": metadata["duration_sec"]},
                        )
                        self.jobs.save(job)
                    stage = "watermark"
                    if job.state is ClipJobState.ASSEMBLED:
                        self.jobs.save(
                            replace(
                                job,
                                state=ClipJobState.PROCESSING,
                                details={**job.details, "media_checkpoint": "ASSEMBLED"},
                            )
                        )
                        metadata = self.media.watermark(job, lock_fd)
                        job = replace(
                            job,
                            state=ClipJobState.WATERMARKED,
                            artifact_location="artifacts/final.mp4",
                            details={**job.details, "media": metadata},
                        )
                        self.jobs.save(job)
                    stage = "thumbnail"
                    self.media.thumbnail(job, lock_fd)
                    self.jobs.save(
                        replace(
                            job,
                            details={**job.details, "thumbnail_complete": True, "last_error": None},
                        )
                    )
                    self.events.emit(
                        "processing.recovered",
                        job=job,
                        stage=(job.details.get("last_error") or {}).get("stage", "processing"),
                        code="media_ready",
                        severity="info",
                        situation="resolved",
                    )
                except Exception as error:
                    self._failed_media(job, stage, error)
                finally:
                    self.running = None
            return

    def _failed_media(self, job: ClipJob, stage: str, error: Exception) -> None:
        blocked = type(error).__name__ == "StorageBlocked"
        attempts = dict(job.details.get("attempts_by_stage", {}))
        if not blocked:
            attempts[stage] = attempts.get(stage, 0) + 1
        terminal = not blocked and attempts[stage] >= job.details.get("policy", {}).get(
            "max_attempts", 3
        )
        code = "insufficient_storage_reserve" if blocked else "media_step_failed"
        failed = replace(
            job,
            state=ClipJobState.BLOCKED
            if blocked
            else (ClipJobState.FAILED if terminal else ClipJobState.RETRY_PENDING),
            attempts=job.attempts + (0 if blocked else 1),
            retry_from=job.state,
            next_attempt_at=None
            if terminal
            else self.clock.now()
            + timedelta(
                seconds=30 if blocked else min(900, 120 * 2 ** min(attempts[stage] - 1, 3))
            ),
            details={
                **job.details,
                "attempts_by_stage": attempts,
                "last_error": {
                    "stage": stage,
                    "code": code,
                    "occurred_at": self.clock.now().isoformat(),
                },
            },
        )
        self.jobs.save(failed)
        self.events.emit("processing.failed", job=failed, stage=stage, code=code, severity="error")

    def deliver_once(self) -> None:
        for saved in self.jobs.all():
            job = self._due(saved)
            if job is None or job.state not in {
                ClipJobState.WATERMARKED,
                ClipJobState.REGISTERED,
                ClipJobState.UPLOADED,
                ClipJobState.FINALIZED,
            }:
                continue
            if job.state is ClipJobState.FINALIZED and job.details.get("cleaned"):
                continue
            if (
                job.state is ClipJobState.FINALIZED
                and job.next_attempt_at
                and job.next_attempt_at > self.clock.now()
            ):
                continue
            if job.state is ClipJobState.WATERMARKED and not job.details.get("thumbnail_complete"):
                continue
            self.jobs.save(job)
            gateway = self.gateway.for_job(job)
            use_case = ProcessClipJob(
                self.jobs,
                self.leases,
                gateway,
                gateway,
                self.artifacts,
                RetryPolicy(
                    job.details.get("policy", {}).get("max_attempts", 3),
                    timedelta(seconds=120),
                    timedelta(seconds=900),
                ),
                self.clock,
                "delivery",
                timedelta(hours=1),
                dev_mode=job.details.get("policy", {}).get("dev", False),
            )
            result = use_case.execute(job.job_id)
            current = self.jobs.get(job.job_id)
            if result.state in {
                ClipJobState.FAILED,
                ClipJobState.RETRY_PENDING,
                ClipJobState.BLOCKED,
            }:
                error = current.details.get("last_error") or {}
                self.events.emit(
                    "delivery.pending",
                    job=current,
                    stage=error.get("stage", "delivery"),
                    code=error.get("code", "delivery_failed"),
                    severity="error",
                )
            elif result.state is ClipJobState.FINALIZED:
                if not current.details.get("cleaned"):
                    self.events.emit(
                        "cleanup.pending",
                        job=current,
                        stage="cleanup",
                        code="cleanup_pending",
                        severity="warning",
                    )
                    return
                self.events.emit(
                    "delivery.finalized",
                    job=current,
                    stage=(job.details.get("last_error") or {}).get("stage", "delivery"),
                    code="finalized",
                    severity="info",
                    situation="resolved",
                )
            return
