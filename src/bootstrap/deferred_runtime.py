"""Concrete wiring for fixed-device deferred preservation and delivery."""

import json
import os
import re
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from src.application.delivery.deferred_coordinator import DeferredCoordinator
from src.application.replay.preserve_replay import PreserveReplay
from src.config.config_loader import get_effective_config
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob, ClipJobState
from src.domain.replay.processing_schedule import DeviceActivity
from src.infrastructure.filesystem.deferred_repository import (
    DeferredJobRepository,
    DeferredLeases,
    LockBusy,
    atomic_json,
    durable_copy,
    exclusive_file,
    require_persistent_storage,
)
from src.infrastructure.http.deferred_gateway import DeferredVideoGateway
from src.infrastructure.media.deferred_media import DeferredMedia
from src.services.mqtt.operational_event_service import OperationalEventService
from src.services.storage_monitor import StorageMonitor
from src.services.storage_retention import prune_finalized_jobs
from src.utils.logger import logger


class DeferredArtifacts:
    def __init__(self, jobs, legacy_roots):
        self.jobs, self.legacy_roots = jobs, legacy_roots

    def preserve(self, job, artifact):
        path = self.jobs.artifact(job, artifact)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("cannot preserve a missing artifact")
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    def cleanup(self, job, artifact):
        if job.state is not ClipJobState.FINALIZED:
            raise ValueError("cleanup requires confirmed finalization")
        for section in ("segments", "artifacts", "assets"):
            directory = self.jobs.artifact(job, section)
            if directory.exists():
                for path in directory.iterdir():
                    safe = self.jobs.artifact(
                        job, str(path.relative_to(self.jobs.directory(job.job_id)))
                    )
                    if safe.is_file():
                        safe.unlink()
        # Legacy originals were kept until remote finalize; never trust arbitrary paths.
        origin = job.details.get("legacy_origin")
        if origin:
            source = Path(origin)
            marker = source.with_suffix(".deferred.json")
            if (
                not source.is_symlink()
                and any(
                    source.resolve().is_relative_to(root.resolve()) for root in self.legacy_roots
                )
                and marker.is_file()
                and json.loads(marker.read_text()).get("job_id") == job.job_id
            ):
                source.unlink(missing_ok=True)
                source.with_suffix(".json").unlink(missing_ok=True)
                for original in job.details.get("legacy_artifacts", []):
                    extra = Path(original)
                    if not extra.is_symlink() and any(
                        extra.resolve().is_relative_to(root.resolve()) for root in self.legacy_roots
                    ):
                        extra.unlink(missing_ok=True)
                # Keep marker as an import tombstone through the rollback window.
        self.jobs.save(
            replace(
                job,
                next_attempt_at=None,
                details={**job.details, "cleaned": True, "last_error": None},
            )
        )


class DeferredRuntime:
    def __init__(
        self,
        *,
        base,
        cameras,
        mqtt_client,
        topic_for,
        identity,
        secret,
        watermark,
        client_watermark,
        top_watermark,
        dev_mode=False,
    ):
        self.base = Path(base)
        self.cameras = cameras
        self.identity = identity
        self.dev_mode = dev_mode
        self.watermarks = (watermark, client_watermark, top_watermark)
        self.root = self.base / "queue_raw" / ".deferred"
        require_persistent_storage(self.root)
        require_persistent_storage(self.base / "runtime_config" / "operational")
        self.jobs = DeferredJobRepository(self.root)
        self._owner = exclusive_file(self.root / "runtime.lock")
        self._owner.__enter__()
        interrupted = self.jobs.list_by_state((ClipJobState.PREPARING, ClipJobState.PROCESSING))
        self.jobs.recover()
        self.events = OperationalEventService(
            self.base / "runtime_config" / "operational", mqtt_client, topic_for, identity, secret
        )
        for original in interrupted:
            recovered = self.jobs.get(original.job_id)
            failed = recovered.state is ClipJobState.FAILED
            self.events.emit(
                "recovery.failed" if failed else "processing.resumed",
                job=recovered,
                stage="preservation" if original.state is ClipJobState.PREPARING else "processing",
                code="interrupted_job_failed" if failed else "checkpoint_recovered",
                severity="error" if failed else "info",
                situation="open" if failed else "resolved",
            )
        self.storage = StorageMonitor(
            [
                self.root,
                self.base / "highlights_wm",
                self.base / "recorded_clips",
                self.events.root,
                Path(os.getenv("GN_LOG_DIR", str(self.base / "logs"))),
            ],
            self.events,
        )
        self.activity = DeviceActivity()
        self.preservation = PreserveReplay(
            self.jobs,
            lambda source, dest: durable_copy(Path(source), dest),
            self.storage,
            self.events,
        )
        self.media = DeferredMedia(self.jobs, self.storage)
        self.legacy_roots = [
            self.base / "queue_raw",
            self.base / "failed_clips",
            self.base / "highlights_wm",
        ]
        self.artifacts = DeferredArtifacts(self.jobs, self.legacy_roots)
        self.coordinator = DeferredCoordinator(
            self.jobs,
            self.media,
            DeferredVideoGateway(self.jobs),
            self.artifacts,
            DeferredLeases(self.jobs),
            lambda: exclusive_file(self.root / "heavy.lock"),
            self.activity,
            self.storage,
            self.events,
            get_effective_config,
            self.preservation,
        )
        self._stop = threading.Event()
        self._threads = []
        self._snapshot = {}

    def policy(self):
        cfg = get_effective_config()
        p = cfg.processing
        return {
            "crf": p.lm_crf if p.light_mode else p.hq_crf,
            "preset": p.lm_preset if p.light_mode else p.hq_preset,
            "vertical_format": p.vertical_format,
            "light_mode": p.light_mode,
            "margin": p.watermark.margin,
            "opacity": p.watermark.opacity,
            "relative_width": p.watermark.relative_width,
            "max_attempts": p.max_attempts,
            "watermark": str(self.watermarks[0]),
            "client_watermark": str(self.watermarks[1]) if self.watermarks[1] else None,
            "top_watermark": str(self.watermarks[2]) if self.watermarks[2] else None,
            "config_version": cfg.config_version,
            "schedule_required": p.deferred_enabled,
            "dev": self.dev_mode,
            "dev_video": os.getenv("DEV_VIDEO_MODE", "").lower() in {"true", "1", "yes"},
        }

    def build_immediate(self, cfg, buffer):
        from src.video.processor import build_highlight

        job = ClipJob(
            "immediate-" + uuid.uuid4().hex,
            CameraId(cfg.camera_id),
            "artifacts/assembled.mp4",
            datetime.now(UTC),
            schema_version=3,
            details={"duration_sec": cfg.pre_seconds + cfg.post_seconds},
        )
        with self.immediate_lock() as descriptor:
            return build_highlight(
                cfg,
                buffer,
                runner=lambda command: self.media.run(command, job=job, lock_fd=descriptor),
            )

    def immediate_lock(self):
        # Serialize legacy concatenation with pending v3 media during rollback.
        from contextlib import contextmanager

        @contextmanager
        def claim():
            while not self._stop.is_set():
                lock = exclusive_file(self.root / "heavy.lock")
                try:
                    descriptor = lock.__enter__()
                    break
                except LockBusy:
                    self._stop.wait(0.1)
            else:
                raise RuntimeError("runtime stopping")
            try:
                yield descriptor
            finally:
                lock.__exit__(None, None, None)

        return claim()

    def record_activity(self, instant=None):
        self.activity.record(instant)

    def admit(self, cfg, buffer, trigger_id, captured_at, triggered_mono):
        return self.preservation.begin(
            cfg,
            buffer,
            trigger_id=trigger_id,
            captured_at=captured_at,
            triggered_mono=triggered_mono,
            identity=self.identity,
            policy=self.policy(),
        )

    def start(self):
        # Migration copies media in the monitor thread, never on the bootstrap
        # path that must finish before capture supervisors can start.
        self.events.start()
        for name, action, interval in (
            ("preserve", self.preservation.tick, 0.1),
            ("media", self.coordinator.process_media_once, 1),
            ("delivery", self.coordinator.deliver_once, 1),
            ("monitor", self._monitor, 30),
        ):
            thread = threading.Thread(
                target=self._loop, args=(action, interval), name=f"deferred-{name}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _loop(self, action, interval):
        while not self._stop.is_set():
            try:
                action()
            except LockBusy:
                pass
            except Exception:
                logger.error("Deferred runtime iteration failed; durable jobs retained")
            self._stop.wait(interval)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(2)
        self.events.stop()
        # If work is still running, keep ownership until process exit.
        if not any(t.is_alive() for t in self._threads):
            self._owner.__exit__(None, None, None)

    def snapshot(self):
        return self._snapshot

    def _monitor(self):
        self._import_legacy()
        removed, bytes_removed = prune_finalized_jobs(
            self.jobs, (self.base / "queue_raw", self.base / "failed_clips")
        )
        if removed:
            logger.info(
                "Retencao: %s manifestos finalizados removidos (%s bytes)",
                removed,
                bytes_removed,
            )
        jobs = self.jobs.all()
        counts = {}
        active = []
        issues = []
        for job in jobs:
            counts[job.state.value] = counts.get(job.state.value, 0) + 1
            if job.state not in {ClipJobState.FINALIZED, ClipJobState.DEV_PRESERVED}:
                active.append(job.created_at)
            if job.state in {
                ClipJobState.FAILED,
                ClipJobState.BLOCKED,
                ClipJobState.RETRY_PENDING,
            } or (job.state is ClipJobState.FINALIZED and job.details.get("last_error")):
                error = job.details.get("last_error") or {}
                issues.append(
                    {
                        "job_id": job.job_id,
                        "camera_id": job.camera_id.value,
                        "captured_at": job.details.get("captured_at"),
                        "state": job.state.value,
                        "stage": error.get("stage"),
                        "code": error.get("code"),
                        "attempts": job.attempts,
                    }
                )
        cfg = get_effective_config()
        self._snapshot = {
            "queues": counts,
            "issues": issues[:50],
            "issues_total": len(issues),
            "issues_truncated": len(issues) > 50,
            "invalid_manifests": self.jobs.invalid_count(),
            "legacy_pending": self.legacy_pending,
            "oldest_pending_age_seconds": max(0, (datetime.now(UTC) - min(active)).total_seconds())
            if active
            else 0,
            "running": self.coordinator.running,
            "volumes": self.storage.poll(),
            "applied_config_version": cfg.config_version,
            "idle_seconds": self.activity.idle_seconds(),
            "deferred_enabled": cfg.processing.deferred_enabled,
        }
        self.events.snapshot(self._snapshot)

    def _import_legacy(self):
        candidates = set()
        self.legacy_pending = 0
        for root in (self.base / "queue_raw", self.base / "failed_clips"):
            for pattern in ("*.mp4", "*.ts"):
                for path in root.rglob(pattern) if root.exists() else ():
                    if ".deferred" not in path.parts and not path.name.endswith(
                        (".partial.mp4", ".wm_tmp.mp4")
                    ):
                        candidates.add(path)
        for source in sorted(candidates):
            meta_path = source.with_suffix(".json")
            if not meta_path.exists() or source.is_symlink():
                self.legacy_pending += 1
                self.events.emit(
                    "migration.blocked", stage="migration", code="legacy_sidecar_missing"
                )
                continue
            job_id = uuid.uuid5(uuid.NAMESPACE_URL, str(source.resolve())).hex
            marker = source.with_suffix(".deferred.json")
            try:
                if marker.exists() and self.jobs.get(job_id):
                    continue
                meta = json.loads(meta_path.read_text())
                if meta.get("schema_version", 1) not in {1, 2}:
                    raise ValueError("unknown legacy format")
                captured = meta.get("captured_at") or meta.get("created_at")
                if not captured:
                    raise ValueError("legacy capture time unavailable")
                created = datetime.fromisoformat(captured.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                captured = created.astimezone(UTC).isoformat()
                state = ClipJobState.ASSEMBLED
                status = meta.get("state") or meta.get("status", "")
                effective_status = meta.get("retry_from") if status == "RETRY_PENDING" else status
                if str(status).lower() in {"dev_local_preserved", "dev_preserved"}:
                    continue
                processed = (
                    isinstance(meta.get("meta_wm"), dict)
                    or bool(meta.get("artifact_location"))
                    or bool(meta.get("wm_encode"))
                )
                final = self.base / "highlights_wm" / source.name
                indicated = meta.get("artifact_location") or meta.get("wm_path")
                if indicated:
                    final = Path(indicated)
                    if not final.is_absolute():
                        final = source.parent / final
                    if final.is_symlink() or not any(
                        final.resolve().is_relative_to(root.resolve()) for root in self.legacy_roots
                    ):
                        raise ValueError("unsafe legacy artifact")
                actual = final if processed and final.exists() else source
                if processed and actual.suffix != ".mp4":
                    raise ValueError("legacy processed container requires inspection")
                if (
                    processed
                    and not final.exists()
                    and source.is_relative_to(self.base / "queue_raw")
                ):
                    raise ValueError("legacy final artifact missing")
                if processed:
                    state = ClipJobState.WATERMARKED
                remote = meta.get("remote_registration", {}).get("response", {})
                clip = remote.get("data", remote)
                clip = clip.get("clip", clip)
                remote_id = meta.get("remote_clip_id") or clip.get("clip_id")
                uploaded = (
                    meta.get("remote_upload", {}).get("status") == "uploaded"
                    or effective_status == "UPLOADED"
                )
                finalized = (
                    meta.get("remote_finalize", {}).get("status") == "ok" or status == "FINALIZED"
                )
                from src.video.processor import _sha256_file

                digest = _sha256_file(actual) if uploaded else None
                expected_digest = meta.get("upload_sha256") or clip.get("sha256")
                expected_size = meta.get("upload_size_bytes") or meta.get("remote_upload", {}).get(
                    "file_size"
                )
                if uploaded and expected_size and int(expected_size) != actual.stat().st_size:
                    raise ValueError("uploaded artifact size mismatch")
                if uploaded and expected_digest and expected_digest != digest:
                    raise ValueError("uploaded artifact integrity mismatch")
                if uploaded and not remote_id:
                    raise ValueError("uploaded legacy identity missing")
                if uploaded:
                    state = ClipJobState.UPLOADED
                if finalized:
                    state = ClipJobState.FINALIZED
                if str(status).upper() == "FAILED":
                    state = ClipJobState.FAILED
                if status == "upload_pending" and not processed:
                    raise ValueError("legacy artifact stage ambiguous")
                policy = self.policy()
                policy["schedule_required"] = get_effective_config().processing.deferred_enabled
                job = ClipJob(
                    job_id,
                    CameraId(self._legacy_camera_id(meta, source)),
                    "artifacts/assembled.mp4",
                    created,
                    state=state,
                    artifact_location="artifacts/final.mp4" if processed or uploaded else None,
                    remote_clip_id=remote_id,
                    upload_size_bytes=actual.stat().st_size if uploaded else None,
                    upload_sha256=digest,
                    upload_etag=meta.get("upload_etag")
                    or meta.get("remote_upload", {}).get("etag"),
                    schema_version=3,
                    details={
                        **self.identity,
                        "captured_at": captured,
                        "timestamp_source": "legacy_metadata",
                        "requested": {
                            key: meta[key]
                            for key in (
                                "pre_seconds",
                                "post_seconds",
                                "pre_segments",
                                "post_segments",
                                "seg_time",
                            )
                            if key in meta
                        },
                        "legacy_origin": str(source.resolve()),
                        "legacy_artifacts": [str(actual.resolve())] if actual != source else [],
                        "source_kind": "assembled_video",
                        "policy": policy,
                        "segments": [],
                        "thumbnail_complete": uploaded or finalized,
                        "attempts_by_stage": {"registration": int(meta.get("attempts", 0))},
                    },
                )
                self.storage.check(self.root, actual.stat().st_size + 8 * 1024 * 1024)
                durable_copy(
                    actual, self.jobs.artifact(job, job.artifact_location or job.source_location)
                )
                if not processed and not uploaded and not finalized:
                    for key in ("watermark", "client_watermark", "top_watermark"):
                        if policy.get(key):
                            relative = f"assets/{key}.png"
                            durable_copy(Path(policy[key]), self.jobs.artifact(job, relative))
                            policy[key] = relative
                    policy["assets_saved"] = True
                atomic_json(marker, {"job_id": job_id})
                self.jobs.save(job)
            except (OSError, ValueError, KeyError, TypeError):
                self.legacy_pending += 1
                self.events.emit(
                    "migration.blocked",
                    stage="migration",
                    code="legacy_import_failed",
                    severity="error",
                )

    def _legacy_camera_id(self, metadata, source):
        if metadata.get("camera_id"):
            return metadata["camera_id"]
        match = re.fullmatch(r"highlight_(.+)_\d{8}-\d{6}-\d{6}Z", source.stem)
        if match:
            return match.group(1)
        cameras = [cfg.camera_id for cfg in self.cameras if cfg.queue_dir == source.parent]
        return cameras[0] if len(cameras) == 1 else "legacy_unknown"
