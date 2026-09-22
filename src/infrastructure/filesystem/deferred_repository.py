"""Version three jobs in the existing queue volume; v2 atomic writer reused."""

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from src.domain.delivery import ClipJob, ClipJobState

from .sidecar_repository import FilesystemClipJobRepository


class LockBusy(RuntimeError):
    pass


def require_persistent_storage(path: Path) -> None:
    """Reject RAM filesystems, including tmpfs mounts outside /dev/shm."""
    resolved = path.resolve()
    best = (0, "")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields, filesystem = line.split(" - ", 1)
        mount = fields.split()[4]
        for escaped, char in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
            mount = mount.replace(escaped, char)
        if resolved.is_relative_to(mount) and len(mount) > best[0]:
            best = (len(mount), filesystem.split()[0])
    if not best[0] or best[1] in {"tmpfs", "ramfs"}:
        raise ValueError("deferred staging requires a persistent filesystem")


@contextmanager
def exclusive_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(path.name) from None
        try:
            yield stream.fileno()
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    FilesystemClipJobRepository._atomic_write(path, payload)


def safe_child(root: Path, relative: str) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute() or any(p in {"..", "."} for p in path.parts):
        raise ValueError("unsafe artifact path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink artifact rejected")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("artifact outside job")
    return current


def durable_copy(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("invalid source file")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    before = source.stat()
    with source.open("rb") as src, temporary.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ) or temporary.stat().st_size != before.st_size:
        raise ValueError("source changed during preservation")
    os.replace(temporary, target)
    sync_directory(target.parent)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DeferredJobRepository:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def directory(self, job_id: str) -> Path:
        if not job_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for c in job_id
        ):
            raise ValueError("invalid job identity")
        return safe_child(self.root, job_id)

    def artifact(self, job: ClipJob, relative: str) -> Path:
        return safe_child(self.directory(job.job_id), relative)

    def save(self, job: ClipJob) -> None:
        payload = {**job.details, **FilesystemClipJobRepository._encode(job)}
        payload["schema_version"] = 3
        atomic_json(self.directory(job.job_id) / "manifest.json", payload)

    def get(self, job_id: str) -> ClipJob | None:
        path = self.directory(job_id) / "manifest.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 3
            or payload.get("job_id") != job_id
        ):
            raise ValueError("unsupported or mismatched manifest")
        policy = payload.get("policy", {})
        attempts = payload.get("attempts_by_stage", {})
        if not isinstance(policy, dict) or not isinstance(attempts, dict):
            raise ValueError("invalid manifest policy or attempts")
        if any(type(n) is not int or n < 0 for n in attempts.values()):
            raise ValueError("invalid attempt counter")
        if "max_attempts" in policy and (
            type(policy["max_attempts"]) is not int or policy["max_attempts"] < 1
        ):
            raise ValueError("invalid attempt limit")
        if payload.get("last_error") is not None and not isinstance(payload["last_error"], dict):
            raise ValueError("invalid error checkpoint")
        if not isinstance(payload.get("segments", []), list):
            raise ValueError("invalid segment set")
        state = ClipJobState(payload["state"])
        job = FilesystemClipJobRepository._decode(path, payload)
        job = replace(job, state=state, schema_version=3, details=payload)
        for location in (job.source_location, job.artifact_location):
            if location:
                self.artifact(job, location)
        for segment in payload.get("segments", []):
            if not isinstance(segment, dict):
                raise ValueError("invalid segment record")
            self.artifact(job, segment["path"])
        if payload.get("policy", {}).get("assets_saved"):
            for key in ("watermark", "client_watermark", "top_watermark"):
                if payload["policy"].get(key):
                    self.artifact(job, payload["policy"][key])
        return job

    def list_by_state(self, states) -> tuple[ClipJob, ...]:
        result = []
        for path in sorted(self.root.glob("*/manifest.json")):
            try:
                job = self.get(path.parent.name)
                if job and job.state in states:
                    result.append(job)
            except (ValueError, KeyError, TypeError, OSError):
                # Preserve unknown/corrupt jobs for inspection; report via snapshot.
                continue
        return tuple(
            sorted(
                result,
                key=lambda job: (
                    job.details.get("captured_at", job.created_at.isoformat()),
                    job.job_id,
                ),
            )
        )

    def all(self) -> tuple[ClipJob, ...]:
        return self.list_by_state(tuple(ClipJobState))

    def invalid_count(self) -> int:
        return sum(1 for p in self.root.iterdir() if p.is_dir()) - len(self.all())

    def recover(self) -> None:
        for job in self.all():
            if job.state is ClipJobState.PREPARING:
                self.save(
                    replace(
                        job,
                        state=ClipJobState.FAILED,
                        details={
                            **job.details,
                            "last_error": {
                                "code": "preservation_interrupted",
                                "stage": "preservation",
                            },
                        },
                    )
                )
            elif job.state is ClipJobState.PROCESSING:
                checkpoint = job.details.get("media_checkpoint", "QUEUED")
                if checkpoint in {"QUEUED", "ASSEMBLED"}:
                    self.save(replace(job, state=ClipJobState(checkpoint)))
                else:
                    self.save(
                        replace(
                            job,
                            state=ClipJobState.FAILED,
                            details={
                                **job.details,
                                "last_error": {
                                    "stage": "recovery",
                                    "code": "invalid_media_checkpoint",
                                },
                            },
                        )
                    )


class DeferredLeases:
    def __init__(self, repository: DeferredJobRepository):
        self.repository = repository

    def acquire(self, job_id, owner_id, ttl):
        return exclusive_file(self.repository.directory(job_id) / "job.lock")
