"""Callable-based bridge from legacy backend helpers to the delivery port."""

from collections.abc import Callable

from src.application.dto import RemoteClipRegistration, UploadReceipt
from src.domain.delivery import ClipJob


class LegacyVideoBackendGateway:
    def __init__(
        self,
        *,
        register: Callable[
            [ClipJob, dict[str, object]],
            RemoteClipRegistration,
        ],
        upload: Callable[[RemoteClipRegistration, str], UploadReceipt],
        finalize: Callable[[str, UploadReceipt], None],
    ) -> None:
        self._register = register
        self._upload = upload
        self._finalize = finalize

    def register(
        self,
        job: ClipJob,
        metadata: dict[str, object],
    ) -> RemoteClipRegistration:
        return self._register(job, metadata)

    def upload(
        self,
        registration: RemoteClipRegistration,
        artifact_location: str,
    ) -> UploadReceipt:
        return self._upload(registration, artifact_location)

    def finalize(self, remote_clip_id: str, receipt: UploadReceipt) -> None:
        self._finalize(remote_clip_id, receipt)
