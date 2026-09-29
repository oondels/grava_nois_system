"""Concrete existing HTTP/S3 transport for durable delivery checkpoints."""

from pathlib import Path

from src.application.dto import RemoteClipRegistration, UploadReceipt
from src.application.exceptions import DeliveryStepError
from src.services.api_client import GravaNoisAPIClient
from src.services.api_error_policy import extract_api_error_from_exception
from src.video.processor import _sha256_file


class DeferredDeliveryError(DeliveryStepError):
    def __init__(self, code, *, retryable=True, blocked=False):
        super().__init__(code, retryable=retryable)
        self.code, self.blocked = code, blocked


def classify(error):
    if isinstance(error, DeferredDeliveryError):
        return error
    info = extract_api_error_from_exception(error)
    if info:
        code = info.error_code or info.message_normalized
        if code == "request_outside_allowed_time_window":
            return DeferredDeliveryError("ingest_window", blocked=True)
        if info.status_code == 409:
            return DeferredDeliveryError("remote_reconciliation_required", blocked=True)
        if info.status_code in {401, 403} or info.should_delete_local_record:
            return DeferredDeliveryError("authorization_rejected", retryable=False)
        if info.status_code in {400, 404, 422}:
            return DeferredDeliveryError("remote_validation_rejected", retryable=False)
    return DeferredDeliveryError("remote_unavailable")


class DeferredVideoGateway:
    def __init__(self, jobs, client=None):
        self.jobs = jobs
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = GravaNoisAPIClient()
        return self._client

    def for_job(self, job):
        parent = self

        class BoundGateway:
            def register(self, current, metadata):
                return parent.register(current, metadata)

            def probe(self, relative):
                from src.video.processor import ffprobe_metadata

                return ffprobe_metadata(parent.jobs.artifact(job, relative))

            def upload(self, registration, relative):
                return parent.upload(registration, str(parent.jobs.artifact(job, relative)))

            def finalize(self, remote_id, receipt):
                return parent.finalize(remote_id, receipt)

        return BoundGateway()

    def register(self, job, metadata):
        try:
            if not self.client.is_configured():
                raise DeferredDeliveryError("api_not_configured", blocked=True)
            if any(
                job.details.get(key) != getattr(self.client, key)
                for key in ("device_id", "client_id", "venue_id")
            ):
                raise DeferredDeliveryError("identity_changed", retryable=False)
            artifact = self.jobs.artifact(job, job.artifact_location)
            digest = _sha256_file(artifact)
            response = self.client.register_clip_metadados(
                {
                    "venue_id": job.details["venue_id"],
                    "captured_at": job.details["captured_at"],
                    "duration_sec": metadata["duration_sec"],
                    "sha256": digest,
                    "dev": job.details["policy"].get("dev_video", False),
                    "meta": {
                        **metadata,
                        "edge_job_id": job.job_id,
                        "config_version": job.details["policy"].get("config_version"),
                    },
                },
                timeout=15,
            )
            clip = self.client.extract_clip_registration(response)
            if not clip.get("clip_id") or not clip.get("upload_url"):
                raise DeferredDeliveryError("registration_incomplete", blocked=True)
            if job.remote_clip_id and clip["clip_id"] != job.remote_clip_id:
                raise DeferredDeliveryError("remote_identity_conflict", blocked=True)
            return RemoteClipRegistration(clip["clip_id"], clip["upload_url"], {})
        except Exception as error:
            raise classify(error) from None

    def upload(self, registration, artifact_location):
        # ProcessClipJob passes a path resolved by the media facade below.
        artifact = Path(artifact_location)
        try:
            status, _, headers = self.client.upload_file_to_signed_url(
                registration.upload_url,
                artifact,
                content_type="video/mp4",
                extra_headers=None,
                timeout=180,
            )
            if not 200 <= status < 300:
                raise DeferredDeliveryError("signed_upload_failed")
            return UploadReceipt(
                status,
                {},
                artifact.stat().st_size,
                _sha256_file(artifact),
                (headers or {}).get("etag"),
            )
        except Exception as error:
            raise classify(error) from None

    def finalize(self, remote_clip_id, receipt):
        try:
            result = self.client.finalize_clip_uploaded(
                clip_id=remote_clip_id,
                size_bytes=receipt.size_bytes,
                sha256=receipt.sha256,
                etag=receipt.etag,
                timeout=20,
            )
            data = result.get("data", result)
            clip = data.get("clip", data)
            if clip.get("clip_id") != remote_clip_id or clip.get("status") not in {
                "uploaded",
                "uploaded_temp",
            }:
                raise DeferredDeliveryError("finalize_not_confirmed", blocked=True)
        except Exception as error:
            raise classify(error) from None
