"""Callable-based artifact adapter for incremental migration."""

from collections.abc import Callable

from src.domain.delivery import ClipJob


class LegacyArtifactStore:
    def __init__(
        self,
        *,
        watermarked_location: Callable[[ClipJob], str],
        preserve: Callable[[ClipJob, str], None],
        discard: Callable[[ClipJob, str], None],
        cleanup: Callable[[ClipJob, str], None],
    ) -> None:
        self._watermarked_location = watermarked_location
        self._preserve = preserve
        self._discard = discard
        self._cleanup = cleanup

    def watermarked_location(self, job: ClipJob) -> str:
        return self._watermarked_location(job)

    def preserve(self, job: ClipJob, artifact_location: str) -> None:
        self._preserve(job, artifact_location)

    def discard(self, job: ClipJob, artifact_location: str) -> None:
        self._discard(job, artifact_location)

    def cleanup(self, job: ClipJob, artifact_location: str) -> None:
        self._cleanup(job, artifact_location)
