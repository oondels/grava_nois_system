"""Durable job storage and remote delivery ports."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Protocol

from src.application.dto import RemoteClipRegistration, UploadReceipt
from src.domain.delivery import ClipJob, ClipJobState


class ClipJobRepository(Protocol):
    def get(self, job_id: str) -> ClipJob | None: ...

    def save(self, job: ClipJob) -> None: ...

    def list_by_state(self, states: Sequence[ClipJobState]) -> Sequence[ClipJob]: ...


class JobLeaseRepository(Protocol):
    def acquire(
        self, job_id: str, owner_id: str, ttl: timedelta
    ) -> AbstractContextManager[None]: ...


class ArtifactStore(Protocol):
    def watermarked_location(self, job: ClipJob) -> str: ...

    def preserve(self, job: ClipJob, artifact_location: str) -> None: ...

    def discard(self, job: ClipJob, artifact_location: str) -> None: ...

    def cleanup(self, job: ClipJob, artifact_location: str) -> None: ...


class VideoBackendGateway(Protocol):
    def register(self, job: ClipJob, metadata: dict[str, object]) -> RemoteClipRegistration: ...

    def upload(
        self, registration: RemoteClipRegistration, artifact_location: str
    ) -> UploadReceipt: ...

    def finalize(self, remote_clip_id: str) -> None: ...
