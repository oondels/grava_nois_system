"""Bounded retention for private managed .env rollback copies."""

from __future__ import annotations

import re
import time
from pathlib import Path

MAX_BACKUPS = 5
MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_REQUEST_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_CONFIG_STAMP = re.compile(r"[0-9]{8}_[0-9]{6}_[0-9]{6}\Z")


def prune_env_backups(env_path: Path, *, now: float | None = None) -> tuple[int, int]:
    """Remove recognized regular copies older than 30 days or beyond the newest five."""
    now = time.time() if now is None else now
    candidates: list[tuple[float, Path, int]] = []
    prefix = env_path.name + ".bak.grn."
    for path in env_path.parent.glob(prefix + "*"):
        suffix = path.name.removeprefix(prefix)
        if not (
            _REQUEST_ID.fullmatch(suffix)
            or (suffix.startswith("config.") and _CONFIG_STAMP.fullmatch(suffix[7:]))
        ):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        candidates.append((stat.st_mtime, path, stat.st_size))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    removed = bytes_removed = 0
    for index, (mtime, path, size) in enumerate(candidates):
        if index < MAX_BACKUPS and now - mtime <= MAX_AGE_SECONDS:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        path.unlink()
        removed += 1
        bytes_removed += size
    return removed, bytes_removed
