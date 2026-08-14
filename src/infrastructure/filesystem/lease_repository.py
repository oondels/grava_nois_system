"""Crash-recoverable filesystem leases for clip jobs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


class LeaseUnavailableError(RuntimeError):
    """Raised when an unexpired lease is already owned."""


class FilesystemJobLeaseRepository:
    def __init__(
        self,
        root: Path,
        *,
        boot_id: str,
        pid: int | None = None,
    ) -> None:
        if not boot_id.strip():
            raise ValueError("boot id must not be empty")
        self._root = root
        self._boot_id = boot_id
        self._pid = os.getpid() if pid is None else pid

    @contextmanager
    def acquire(
        self,
        job_id: str,
        owner_id: str,
        ttl: timedelta,
    ) -> Iterator[None]:
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
        if not owner_id.strip():
            raise ValueError("owner id must not be empty")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._safe_path(job_id)
        token = uuid4().hex
        acquired_at = datetime.now(UTC)
        payload = {
            "job_id": job_id,
            "owner_id": owner_id,
            "boot_id": self._boot_id,
            "pid": self._pid,
            "acquired_at": acquired_at.isoformat(),
            "ttl_seconds": ttl.total_seconds(),
            "token": token,
        }
        self._claim(path, payload, acquired_at)
        try:
            yield
        finally:
            self._release_if_owned(path, token)

    def _claim(
        self,
        path: Path,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        for _ in range(2):
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if not self._remove_if_expired(path, now):
                    raise LeaseUnavailableError(f"job lease is already held: {path.stem}") from None
                continue
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            return
        raise LeaseUnavailableError(f"could not acquire job lease: {path.stem}")

    @staticmethod
    def _remove_if_expired(path: Path, now: datetime) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            acquired = datetime.fromisoformat(str(payload["acquired_at"]).replace("Z", "+00:00"))
            expires_at = acquired + timedelta(seconds=float(payload["ttl_seconds"]))
            if now < expires_at:
                return False
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True

    @staticmethod
    def _release_if_owned(path: Path, token: str) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("token") == token:
                path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            return

    def _safe_path(self, job_id: str) -> Path:
        if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
            raise ValueError("job id must be a safe file name")
        return self._root / f"{job_id}.lease"
