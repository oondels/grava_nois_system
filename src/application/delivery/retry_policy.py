"""Deterministic retry decisions for clip delivery."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.domain.delivery import ClipJob, ClipJobState, RetryDecision
from src.domain.exceptions import InvariantViolation


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay: timedelta
    max_delay: timedelta

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise InvariantViolation("max attempts must be positive")
        if self.base_delay.total_seconds() <= 0:
            raise InvariantViolation("base retry delay must be positive")
        if self.max_delay < self.base_delay:
            raise InvariantViolation("max retry delay must not be shorter than base delay")

    def decide(
        self,
        job: ClipJob,
        *,
        now: datetime,
        retryable: bool = True,
    ) -> RetryDecision:
        next_attempt_number = job.attempts + 1
        if not retryable:
            return RetryDecision(False, reason="non_retryable")
        if next_attempt_number >= self.max_attempts:
            return RetryDecision(False, reason="attempts_exhausted")

        multiplier = 2 ** (next_attempt_number - 1)
        delay = min(self.base_delay * multiplier, self.max_delay)
        return RetryDecision(True, now + delay, "retryable")

    def record_failure(
        self,
        job: ClipJob,
        *,
        now: datetime,
        retryable: bool = True,
    ) -> ClipJob:
        decision = self.decide(job, now=now, retryable=retryable)
        state = ClipJobState.RETRY_PENDING if decision.should_retry else ClipJobState.FAILED
        return job.record_failure(
            next_state=state,
            next_attempt_at=decision.next_attempt_at,
        )

    @staticmethod
    def is_due(job: ClipJob, *, now: datetime) -> bool:
        return (
            job.state is ClipJobState.RETRY_PENDING
            and job.next_attempt_at is not None
            and now >= job.next_attempt_at
        )
