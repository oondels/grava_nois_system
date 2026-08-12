"""Filesystem persistence for versioned clip-job sidecars."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState

_LEGACY_TO_STATE = {
    "queued": ClipJobState.QUEUED,
    "queued_retry": ClipJobState.RETRY_PENDING,
    "retry_uploading": ClipJobState.PROCESSING,
    "watermarked": ClipJobState.WATERMARKED,
    "registered": ClipJobState.REGISTERED,
    "uploaded": ClipJobState.UPLOADED,
    "finalized": ClipJobState.FINALIZED,
    "upload_pending": ClipJobState.RETRY_PENDING,
    "failed": ClipJobState.FAILED,
    "skipped": ClipJobState.DISCARDED,
    "dev_local_preserved": ClipJobState.DEV_PRESERVED,
}

_STATE_TO_LEGACY = {
    ClipJobState.QUEUED: "queued",
    ClipJobState.PROCESSING: "processing",
    ClipJobState.WATERMARKED: "watermarked",
    ClipJobState.REGISTERED: "registered",
    ClipJobState.UPLOADED: "uploaded",
    ClipJobState.FINALIZED: "finalized",
    ClipJobState.RETRY_PENDING: "queued_retry",
    ClipJobState.FAILED: "failed",
    ClipJobState.DISCARDED: "discarded",
    ClipJobState.DEV_PRESERVED: "dev_local_preserved",
}


class FilesystemClipJobRepository:
    """Store v2 fields alongside, rather than instead of, legacy metadata."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def get(self, job_id: str) -> ClipJob | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        payload = self._read_payload(path)
        return None if payload is None else self._decode_or_quarantine(path, payload)

    def save(self, job: ClipJob) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(job.job_id)
        existing = self._read_payload(path) if path.exists() else {}
        payload = existing if existing is not None else {}
        # Signed upload credentials are short-lived secrets and must never survive
        # a checkpoint, including when upgrading a legacy sidecar in place.
        _strip_signed_upload_credentials(payload)
        payload.update(self._encode(job))
        self._atomic_write(path, payload)

    def list_by_state(self, states: Sequence[ClipJobState]) -> Sequence[ClipJob]:
        if not self._root.exists():
            return ()
        expected = frozenset(states)
        jobs: list[ClipJob] = []
        for path in sorted(self._root.glob("*.json")):
            payload = self._read_payload(path)
            if payload is None:
                continue
            job = self._decode_or_quarantine(path, payload)
            if job is None:
                continue
            if job.state in expected:
                jobs.append(job)
        return tuple(jobs)

    def _decode_or_quarantine(
        self,
        path: Path,
        payload: dict[str, Any],
    ) -> ClipJob | None:
        try:
            return self._decode(path, payload)
        except (OSError, TypeError, ValueError):
            self._quarantine(path)
            return None

    def _path(self, job_id: str) -> Path:
        if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
            raise ValueError("job id must be a safe file name")
        return self._root / f"{job_id}.json"

    def _read_payload(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("sidecar root must be an object")
            return value
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            self._quarantine(path)
            return None

    def _quarantine(self, path: Path) -> None:
        if not path.exists():
            return
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = path.with_name(f"{path.name}.corrupt-{suffix}-{uuid4().hex}")
        with suppress(FileNotFoundError):
            os.replace(path, quarantine)

    @staticmethod
    def _decode(path: Path, payload: dict[str, Any]) -> ClipJob:
        raw_state = str(payload.get("state") or payload.get("status") or "queued")
        try:
            state = ClipJobState(raw_state.upper())
        except ValueError:
            state = _LEGACY_TO_STATE.get(raw_state.lower(), ClipJobState.QUEUED)
        remote_finalize = payload.get("remote_finalize")
        if isinstance(remote_finalize, dict) and remote_finalize.get("status") == "ok":
            state = ClipJobState.FINALIZED

        created_at = _parse_datetime(payload.get("created_at")) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=UTC
        )
        next_attempt_at = _parse_datetime(payload.get("next_attempt_at"))
        source = str(
            payload.get("source_location") or payload.get("file_name") or path.with_suffix(".mp4")
        )
        camera_id = str(payload.get("camera_id") or payload.get("cameraId") or "legacy")
        return ClipJob(
            job_id=str(payload.get("job_id") or path.stem),
            camera_id=CameraId(camera_id),
            source_location=source,
            created_at=created_at,
            state=state,
            attempts=max(0, int(payload.get("attempts") or 0)),
            next_attempt_at=next_attempt_at,
            retry_from=_parse_state(payload.get("retry_from")),
            artifact_location=_optional_string(payload.get("artifact_location")),
            remote_clip_id=_optional_string(payload.get("remote_clip_id")),
            upload_size_bytes=_optional_positive_int(payload.get("upload_size_bytes")),
            upload_sha256=_optional_sha256(payload.get("upload_sha256")),
            upload_etag=_optional_string(payload.get("upload_etag")),
        )

    @staticmethod
    def _encode(job: ClipJob) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "job_id": job.job_id,
            "camera_id": job.camera_id.value,
            "source_location": job.source_location,
            "created_at": job.created_at.isoformat(),
            "state": job.state.value,
            "status": _STATE_TO_LEGACY[job.state],
            "attempts": job.attempts,
            "next_attempt_at": (
                job.next_attempt_at.isoformat() if job.next_attempt_at is not None else None
            ),
            "retry_from": job.retry_from.value if job.retry_from is not None else None,
            "artifact_location": job.artifact_location,
            "remote_clip_id": job.remote_clip_id,
            "upload_size_bytes": job.upload_size_bytes,
            "upload_sha256": job.upload_sha256,
            "upload_etag": job.upload_etag,
        }

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_state(value: object) -> ClipJobState | None:
    try:
        return ClipJobState(str(value))
    except ValueError:
        return None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _optional_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return None
    return normalized


def _strip_signed_upload_credentials(value: object) -> None:
    if isinstance(value, dict):
        for key in tuple(value):
            if str(key).lower() in {
                "upload_url",
                "signed_upload_url",
                "presigned_url",
                "upload_headers",
            }:
                value.pop(key, None)
                continue
            _strip_signed_upload_credentials(value[key])
    elif isinstance(value, list):
        for item in value:
            _strip_signed_upload_credentials(item)
