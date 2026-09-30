"""Conservative retention of terminal local storage artifacts."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from src.domain.delivery import ClipJobState
from src.infrastructure.filesystem.deferred_repository import LockBusy, exclusive_file

RETENTION_SECONDS = 30 * 24 * 60 * 60
_RETRY_STATES = {"upload_pending", "queued_retry", "watermarked", "ready_for_upload"}


def _safe_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _last_activity(meta: dict, *paths: Path) -> float | None:
    try:
        last = max(path.stat().st_mtime for path in paths)
        if meta.get("updated_at"):
            stamp = datetime.fromisoformat(str(meta["updated_at"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
            last = max(last, stamp.timestamp())
        return last
    except (OSError, ValueError, TypeError):
        return None


def prune_legacy_failed(
    failed_dir: Path, *, max_attempts: int, now: float | None = None
) -> tuple[int, int]:
    """Expire only evidenced terminal clips from the active legacy worker."""
    now = time.time() if now is None else now
    removed = bytes_removed = 0
    for directory in (failed_dir, failed_dir / "upload_failed"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for video in (*directory.glob("*.mp4"), *directory.glob("*.ts")):
            sidecar = video.with_suffix(".json")
            error = video.with_suffix(".error.txt")
            if (
                not _safe_file(video)
                or not _safe_file(sidecar)
                or video.with_suffix(".deferred.json").exists()
                or video.with_suffix(".lock").exists()
            ):
                continue
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    continue
                attempts = int(meta.get("attempts", -1))
                status = meta.get("status")
                terminal = status == "failed" or (
                    directory.name == "upload_failed"
                    and status in _RETRY_STATES
                    and attempts >= max_attempts
                )
                if not terminal or (meta.get("remote_finalize") or {}).get("status") == "ok":
                    continue
                last = _last_activity(meta, video, sidecar)
                if last is None or now - last < RETENTION_SECONDS:
                    continue
                files = [video, sidecar]
                if _safe_file(error):
                    files.append(error)
                size = sum(path.stat().st_size for path in files)
                # Delete media first: an interrupted cleanup leaves a recognizable sidecar.
                for path in files:
                    path.unlink()
                removed += 1
                bytes_removed += size
            except (OSError, ValueError, TypeError, AttributeError):
                # Unknown or concurrently changed items require manual inspection.
                continue
    return removed, bytes_removed


def prune_finalized_jobs(
    jobs, legacy_roots: tuple[Path, ...], *, now: float | None = None
) -> tuple[int, int]:
    """Expire metadata only for cleaned, remotely finalized v3 jobs."""
    now = time.time() if now is None else now
    removed = bytes_removed = 0
    for manifest in jobs.root.glob("*/manifest.json"):
        directory = manifest.parent
        if directory.is_symlink() or not _safe_file(manifest):
            continue
        if now - manifest.stat().st_mtime < RETENTION_SECONDS:
            continue
        try:
            with exclusive_file(directory / "job.lock"):
                job = jobs.get(directory.name)
                if (
                    job is None
                    or job.state is not ClipJobState.FINALIZED
                    or job.details.get("cleaned") is not True
                    or now - manifest.stat().st_mtime < RETENTION_SECONDS
                ):
                    continue
                marker = None
                origin = job.details.get("legacy_origin")
                if origin:
                    source = Path(origin)
                    if (
                        source.is_symlink()
                        or source.exists()
                        or ".deferred" in source.parts
                        or not any(
                            source.resolve().is_relative_to(root.resolve()) for root in legacy_roots
                        )
                    ):
                        continue
                    marker = source.with_suffix(".deferred.json")
                    if marker.exists() and (
                        not _safe_file(marker)
                        or json.loads(marker.read_text()).get("job_id") != job.job_id
                    ):
                        continue
                for child in directory.iterdir():
                    if child.name in {"manifest.json", "job.lock"} and _safe_file(child):
                        continue
                    if (
                        child.name in {"segments", "artifacts", "assets"}
                        and child.is_dir()
                        and not child.is_symlink()
                        and not any(child.iterdir())
                    ):
                        continue
                    break
                else:
                    size = manifest.stat().st_size + (
                        marker.stat().st_size if marker and marker.exists() else 0
                    )
                    if marker and marker.exists():
                        marker.unlink()
                    manifest.unlink()
                    (directory / "job.lock").unlink()
                    for name in ("segments", "artifacts", "assets"):
                        child = directory / name
                        if child.is_dir():
                            child.rmdir()
                    directory.rmdir()
                    removed += 1
                    bytes_removed += size
        except (OSError, ValueError, KeyError, TypeError, AttributeError, LockBusy):
            continue
    return removed, bytes_removed
