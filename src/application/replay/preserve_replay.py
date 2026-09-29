"""Incremental preservation independent of media processing and network delivery."""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState


class PreserveReplay:
    def __init__(
        self,
        repository: Any,
        copy_file: Any,
        storage: Any,
        events: Any,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.jobs = repository
        self.copy_file = copy_file
        self.storage = storage
        self.events = events
        self.clock = monotonic
        self.pending: dict[str, tuple[Any, float, float, float]] = {}
        self._lock = threading.RLock()

    def begin(
        self,
        cfg: Any,
        buffer: Any,
        *,
        trigger_id: str,
        captured_at: str,
        triggered_mono: float,
        identity: dict[str, Any],
        policy: dict[str, Any],
    ) -> str:
        job_id = uuid.uuid4().hex
        pre = cfg.pre_segments * cfg.seg_time if cfg.pre_segments is not None else cfg.pre_seconds
        post = (
            cfg.post_segments * cfg.seg_time if cfg.post_segments is not None else cfg.post_seconds
        )
        start, end = triggered_mono - pre, triggered_mono + post
        # Must precede disk work AND the wait for post-buffer.
        previous = buffer.protect(job_id, start, end)
        job = ClipJob(
            job_id,
            CameraId(cfg.camera_id),
            "artifacts/assembled.mp4",
            datetime.now(UTC),
            state=ClipJobState.PREPARING,
            schema_version=3,
            details={
                **identity,
                "trigger_id": trigger_id,
                "captured_at": captured_at,
                "timestamp_source": "trigger_received",
                "source_kind": "segments",
                "requested": {
                    "pre_seconds": pre,
                    "post_seconds": post,
                    "pre_segments": cfg.pre_segments,
                    "post_segments": cfg.post_segments,
                    "segment_seconds": cfg.seg_time,
                },
                "segments": [],
                "policy": policy,
                "attempts_by_stage": {},
                "capture_session_id": cfg.capture_session_id,
                "timing_source": "segment_csv_local_monotonic_anchor",
            },
        )
        try:
            estimate = sum(seg["size_bytes"] for seg in previous)
            # Conservative bootstrap estimate for post-buffer, including asset copies.
            estimate += int(estimate * post / max(pre, 1)) + 8 * 1024 * 1024
            self.storage.reserve(job_id, self.jobs.directory(job_id), estimate)
            self.jobs.save(job)
            with self._lock:
                self.pending[job_id] = (buffer, start, end, end + max(30, 3 * cfg.seg_time))
        except Exception:
            buffer.release(job_id)
            self.storage.release(job_id)
            self.events.emit(
                "capture.not_preserved",
                job=job,
                stage="preservation",
                code="preservation_admission_failed",
                severity="error",
            )
            raise
        return job_id

    def tick(self) -> None:
        with self._lock:
            pending = tuple(self.pending.items())
        for job_id, (buffer, start, end, deadline) in pending:
            job = None
            try:
                job = self.jobs.get(job_id)
                if job is None:
                    raise ValueError("missing manifest")
                segments = list(job.details["segments"])
                for segment in buffer.pending_segments(job_id):
                    target = self.jobs.artifact(job, "segments/" + segment["name"])
                    self.storage.check(target, segment["size_bytes"], owner=job_id)
                    self.copy_file(segment["source"], target)
                    saved = {
                        k: v
                        for k, v in segment.items()
                        if k not in {"source", "start_mono", "end_mono"}
                    }
                    saved.update(
                        path="segments/" + segment["name"],
                        start_offset=segment["start_mono"] - start,
                        end_offset=segment["end_mono"] - start,
                    )
                    segments.append(saved)
                    segments.sort(key=lambda item: item["media_start"])
                    job = replace(job, details={**job.details, "segments": segments})
                    self.jobs.save(job)
                    buffer.copied(job_id, segment["source"])
                # Assets are snapshotted before eligibility, so later config cannot change a replay.
                policy = dict(job.details["policy"])
                if not policy.get("assets_saved"):
                    for key in ("watermark", "client_watermark", "top_watermark"):
                        source = policy.get(key)
                        if source:
                            target = self.jobs.artifact(job, f"assets/{key}.png")
                            self.storage.check(target, 8 * 1024 * 1024, owner=job_id)
                            self.copy_file(source, target)
                            policy[key] = f"assets/{key}.png"
                    policy["assets_saved"] = True
                    job = replace(job, details={**job.details, "policy": policy})
                    self.jobs.save(job)
                if self._complete(segments, end - start):
                    job = replace(
                        job,
                        state=ClipJobState.QUEUED,
                        details={
                            **job.details,
                            "preserved_interval": {
                                "start_offset": segments[0]["start_offset"],
                                "end_offset": segments[-1]["end_offset"],
                            },
                        },
                    )
                    self.jobs.save(job)
                    self.events.emit(
                        "capture.preserved",
                        job=job,
                        stage="preservation",
                        code="preserved",
                        severity="info",
                        situation="resolved",
                    )
                    self._release(job_id, buffer)
                elif self.clock() >= deadline or buffer._stop.is_set():
                    raise ValueError("incomplete post-buffer")
            except Exception as error:
                if job:
                    code = (
                        "preservation_incomplete"
                        if isinstance(error, ValueError)
                        else "preservation_io_failed"
                    )
                    job = replace(
                        job,
                        state=ClipJobState.FAILED,
                        details={
                            **job.details,
                            "last_error": {
                                "code": code,
                                "stage": "preservation",
                                "occurred_at": datetime.now(UTC).isoformat(),
                            },
                        },
                    )
                    try:
                        self.jobs.save(job)
                    except OSError:
                        logging.getLogger(__name__).error(
                            "Preservation failure could not be persisted"
                        )
                    finally:
                        self._release(job_id, buffer)
                    self.events.emit(
                        "capture.not_preserved",
                        job=job,
                        stage="preservation",
                        code=code,
                        severity="error",
                    )
                else:
                    self._release(job_id, buffer)
                    self.events.emit(
                        "capture.not_preserved",
                        stage="preservation",
                        code="manifest_unreadable",
                        severity="error",
                    )

    @staticmethod
    def _complete(segments: list[dict[str, Any]], duration: float) -> bool:
        if (
            not segments
            or segments[0]["start_offset"] > 0.04
            or segments[-1]["end_offset"] < duration - 0.04
        ):
            return False
        return all(
            a["session_id"] == b["session_id"] and b["start_offset"] <= a["end_offset"] + 0.04
            for a, b in zip(segments, segments[1:], strict=False)
        )

    def _release(self, job_id: str, buffer: Any) -> None:
        buffer.release(job_id)
        self.storage.release(job_id)
        with self._lock:
            self.pending.pop(job_id, None)
