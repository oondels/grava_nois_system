"""Business vocabulary for durable clip delivery."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from src.domain.capture import CameraId
from src.domain.exceptions import InvalidStateTransition, InvariantViolation


class ClipJobState(str, Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    WATERMARKED = "WATERMARKED"
    REGISTERED = "REGISTERED"
    UPLOADED = "UPLOADED"
    FINALIZED = "FINALIZED"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    DISCARDED = "DISCARDED"
    DEV_PRESERVED = "DEV_PRESERVED"


_ALLOWED_TRANSITIONS: dict[ClipJobState, frozenset[ClipJobState]] = {
    ClipJobState.QUEUED: frozenset({ClipJobState.PROCESSING, ClipJobState.DISCARDED}),
    ClipJobState.PROCESSING: frozenset(
        {
            ClipJobState.WATERMARKED,
            ClipJobState.RETRY_PENDING,
            ClipJobState.FAILED,
            ClipJobState.DEV_PRESERVED,
        }
    ),
    ClipJobState.WATERMARKED: frozenset(
        {
            ClipJobState.REGISTERED,
            ClipJobState.RETRY_PENDING,
            ClipJobState.FAILED,
            ClipJobState.DEV_PRESERVED,
        }
    ),
    ClipJobState.REGISTERED: frozenset(
        {ClipJobState.UPLOADED, ClipJobState.RETRY_PENDING, ClipJobState.FAILED}
    ),
    ClipJobState.UPLOADED: frozenset(
        {ClipJobState.FINALIZED, ClipJobState.RETRY_PENDING, ClipJobState.FAILED}
    ),
    ClipJobState.RETRY_PENDING: frozenset(
        {ClipJobState.PROCESSING, ClipJobState.FAILED, ClipJobState.DISCARDED}
    ),
    ClipJobState.FAILED: frozenset({ClipJobState.RETRY_PENDING, ClipJobState.DISCARDED}),
    ClipJobState.FINALIZED: frozenset(),
    ClipJobState.DISCARDED: frozenset(),
    ClipJobState.DEV_PRESERVED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ClipJob:
    job_id: str
    camera_id: CameraId
    source_location: str
    created_at: datetime
    state: ClipJobState = ClipJobState.QUEUED
    attempts: int = 0
    next_attempt_at: datetime | None = None
    retry_from: ClipJobState | None = None
    artifact_location: str | None = None
    remote_clip_id: str | None = None
    upload_size_bytes: int | None = None
    upload_sha256: str | None = None
    upload_etag: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise InvariantViolation("job id must not be empty")
        if not self.source_location.strip():
            raise InvariantViolation("job source location must not be empty")
        if self.attempts < 0:
            raise InvariantViolation("job attempts must not be negative")
        if self.upload_size_bytes is not None and self.upload_size_bytes <= 0:
            raise InvariantViolation("upload size must be positive")
        if self.upload_sha256 is not None and not _is_sha256(self.upload_sha256):
            raise InvariantViolation("upload sha256 must be a hexadecimal digest")

    def transition_to(self, state: ClipJobState) -> "ClipJob":
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransition(f"{self.state.value} -> {state.value}")
        return replace(self, state=state)

    def record_failure(
        self,
        *,
        next_state: ClipJobState,
        next_attempt_at: datetime | None,
    ) -> "ClipJob":
        """Record exactly one completed attempt and its resulting state."""
        if next_state not in {ClipJobState.RETRY_PENDING, ClipJobState.FAILED}:
            raise InvariantViolation("a failed attempt must become retry pending or failed")
        if next_state is ClipJobState.RETRY_PENDING and next_attempt_at is None:
            raise InvariantViolation("a retry pending job must have a next attempt time")
        if next_state is ClipJobState.FAILED and next_attempt_at is not None:
            raise InvariantViolation("a failed job must not have a next attempt time")
        if next_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransition(f"{self.state.value} -> {next_state.value}")
        return replace(
            self,
            state=next_state,
            attempts=self.attempts + 1,
            next_attempt_at=next_attempt_at,
            retry_from=self.state if next_state is ClipJobState.RETRY_PENDING else None,
        )

    def begin_retry(self) -> "ClipJob":
        """Resume a due retry without incrementing the attempt counter."""
        if self.state is not ClipJobState.RETRY_PENDING or self.retry_from is None:
            raise InvalidStateTransition(f"{self.state.value} -> {ClipJobState.PROCESSING.value}")
        if self.retry_from not in {
            ClipJobState.PROCESSING,
            ClipJobState.WATERMARKED,
            ClipJobState.REGISTERED,
            ClipJobState.UPLOADED,
        }:
            raise InvalidStateTransition(
                f"{ClipJobState.RETRY_PENDING.value} -> {self.retry_from.value}"
            )
        return replace(
            self,
            state=self.retry_from,
            next_attempt_at=None,
            retry_from=None,
        )

    def with_artifact(self, location: str) -> "ClipJob":
        if not location.strip():
            raise InvariantViolation("artifact location must not be empty")
        return replace(
            self.transition_to(ClipJobState.WATERMARKED),
            artifact_location=location,
        )

    def with_registration(
        self,
        *,
        remote_clip_id: str,
    ) -> "ClipJob":
        if not remote_clip_id.strip():
            raise InvariantViolation("remote clip id must not be empty")
        return replace(
            self.transition_to(ClipJobState.REGISTERED),
            remote_clip_id=remote_clip_id,
        )

    def with_upload_receipt(
        self,
        *,
        size_bytes: int,
        sha256: str,
        etag: str | None,
    ) -> "ClipJob":
        if size_bytes <= 0:
            raise InvariantViolation("upload size must be positive")
        if not _is_sha256(sha256):
            raise InvariantViolation("upload sha256 must be a hexadecimal digest")
        return replace(
            self.transition_to(ClipJobState.UPLOADED),
            upload_size_bytes=size_bytes,
            upload_sha256=sha256.lower(),
            upload_etag=etag,
        )

    def refresh_registration(self, remote_clip_id: str) -> "ClipJob":
        if self.state is not ClipJobState.REGISTERED:
            raise InvalidStateTransition(f"{self.state.value} -> {ClipJobState.REGISTERED.value}")
        if not remote_clip_id.strip():
            raise InvariantViolation("remote clip id must not be empty")
        return replace(self, remote_clip_id=remote_clip_id)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    next_attempt_at: datetime | None = None
    reason: str = ""


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )
