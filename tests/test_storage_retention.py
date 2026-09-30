from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from src.config.env_backup_retention import MAX_AGE_SECONDS, prune_env_backups
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState
from src.infrastructure.filesystem.deferred_repository import DeferredJobRepository
from src.services.storage_retention import (
    RETENTION_SECONDS,
    prune_finalized_jobs,
    prune_legacy_failed,
)


class StorageRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now(UTC).timestamp()

    def _old(self, path: Path, age: int = RETENTION_SECONDS + 10) -> None:
        os.utime(path, (self.now - age, self.now - age))

    def test_env_backups_keep_newest_five_and_expire_old_copies(self) -> None:
        env = self.root / ".env"
        env.write_text("ITEM=current\n")
        backups = []
        for number in range(7):
            backup = self.root / f".env.bak.grn.00000000-0000-0000-0000-{number:012d}"
            backup.write_text("old\n")
            os.utime(backup, (self.now - number, self.now - number))
            backups.append(backup)
        stale = self.root / ".env.bak.grn.config.20200101_000000_000000"
        stale.write_text("old\n")
        self._old(stale, MAX_AGE_SECONDS + 10)
        unrelated = self.root / ".env.bak.grn.keep"
        unrelated.write_text("keep\n")
        link = self.root / ".env.bak.grn.ffffffff-ffff-ffff-ffff-ffffffffffff"
        link.symlink_to(env)

        removed, _ = prune_env_backups(env, now=self.now)

        self.assertEqual(removed, 3)
        self.assertTrue(all(path.exists() for path in backups[:5]))
        self.assertTrue(all(not path.exists() for path in backups[5:]))
        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(env.read_text(), "ITEM=current\n")

    def test_legacy_cleanup_only_removes_old_terminal_items(self) -> None:
        failed = self.root / "failed_clips"
        upload_failed = failed / "upload_failed"
        upload_failed.mkdir(parents=True)
        for stem, directory, status, attempts in (
            ("terminal", failed, "failed", 3),
            ("exhausted", upload_failed, "upload_pending", 3),
            ("pending", upload_failed, "upload_pending", 2),
            ("deferred", failed, "failed", 3),
            ("recent", failed, "failed", 3),
        ):
            video = directory / f"{stem}.mp4"
            sidecar = directory / f"{stem}.json"
            video.write_bytes(b"media")
            sidecar.write_text(json.dumps({"status": status, "attempts": attempts}))
            if stem != "recent":
                self._old(video)
                self._old(sidecar)
        (failed / "deferred.deferred.json").write_text('{"job_id":"live"}')

        removed, bytes_removed = prune_legacy_failed(failed, max_attempts=3, now=self.now)

        self.assertEqual(removed, 2)
        self.assertGreater(bytes_removed, 0)
        self.assertFalse((failed / "terminal.mp4").exists())
        self.assertFalse((upload_failed / "exhausted.mp4").exists())
        self.assertTrue((upload_failed / "pending.mp4").exists())
        self.assertTrue((failed / "deferred.mp4").exists())
        self.assertTrue((failed / "recent.mp4").exists())

    def test_deferred_cleanup_requires_finalized_cleaned_and_empty_artifacts(self) -> None:
        repo = DeferredJobRepository(self.root / "queue_raw" / ".deferred")
        old = ClipJob(
            "completed",
            CameraId("camera"),
            "artifacts/clip.mp4",
            datetime.now(UTC),
            state=ClipJobState.FINALIZED,
            details={"cleaned": True},
        )
        failed = replace(old, job_id="failed", state=ClipJobState.FAILED)
        unknown = replace(old, job_id="unknown")
        for job in (old, failed, unknown):
            repo.save(job)
            self._old(repo.directory(job.job_id) / "manifest.json")
        (repo.directory("unknown") / "unexpected.bin").write_bytes(b"keep")

        removed, _ = prune_finalized_jobs(
            repo, (self.root / "queue_raw", self.root / "failed_clips"), now=self.now
        )

        self.assertEqual(removed, 1)
        self.assertFalse(repo.directory("completed").exists())
        self.assertTrue(repo.directory("failed").exists())
        self.assertTrue(repo.directory("unknown").exists())

    def test_deferred_cleanup_removes_matching_legacy_tombstone(self) -> None:
        queue = self.root / "queue_raw"
        failed = self.root / "failed_clips"
        failed.mkdir()
        origin = failed / "original.mp4"
        marker = origin.with_suffix(".deferred.json")
        marker.write_text(json.dumps({"job_id": "completed"}))
        repo = DeferredJobRepository(queue / ".deferred")
        job = ClipJob(
            "completed",
            CameraId("camera"),
            "artifacts/clip.mp4",
            datetime.now(UTC),
            state=ClipJobState.FINALIZED,
            details={"cleaned": True, "legacy_origin": str(origin)},
        )
        repo.save(job)
        self._old(repo.directory(job.job_id) / "manifest.json")

        removed, _ = prune_finalized_jobs(repo, (queue, failed), now=self.now)

        self.assertEqual(removed, 1)
        self.assertFalse(marker.exists())
        self.assertFalse(repo.directory(job.job_id).exists())


if __name__ == "__main__":
    unittest.main()
