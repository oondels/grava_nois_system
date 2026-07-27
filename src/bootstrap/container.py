"""Explicit composition root for the incrementally migrated application."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType

from src.application.capture import CameraSupervisionCoordinator, CameraSupervisor
from src.application.configuration.snapshot import SystemSnapshot
from src.application.delivery import ProcessClipJob, RetryPolicy
from src.application.ports import (
    ArtifactStore,
    CaptureProcess,
    ClipJobRepository,
    Clock,
    JobLeaseRepository,
    MediaTool,
    OperationalConfigRepository,
    SegmentRepository,
    VideoBackendGateway,
)
from src.application.replay import CaptureReplay
from src.bootstrap.runtime import EdgeRuntime, LifecycleComponent
from src.domain.capture import CameraId
from src.domain.configuration import DeviceIdentity


@dataclass(frozen=True, slots=True)
class CameraBinding:
    """Infrastructure capabilities owned by one configured camera."""

    camera_id: str
    process: CaptureProcess
    segments: SegmentRepository
    media: MediaTool


@dataclass(frozen=True, slots=True)
class DeliveryBinding:
    """Capabilities required to compose the durable delivery use case."""

    jobs: ClipJobRepository
    leases: JobLeaseRepository
    media: MediaTool
    backend: VideoBackendGateway
    artifacts: ArtifactStore
    retry_policy: RetryPolicy
    clock: Clock
    owner_id: str
    lease_ttl: timedelta


@dataclass(frozen=True, slots=True)
class SystemContainer:
    """Typed application graph; it is intentionally not a service locator."""

    snapshot: SystemSnapshot
    runtime: EdgeRuntime
    capture_replays: Mapping[str, CaptureReplay]
    camera_supervision: CameraSupervisionCoordinator
    process_clip_job: ProcessClipJob | None


def build_container(
    *,
    config_repository: OperationalConfigRepository,
    identity: DeviceIdentity,
    clock: Clock,
    cameras: tuple[CameraBinding, ...] = (),
    delivery: DeliveryBinding | None = None,
    lifecycle_components: tuple[LifecycleComponent, ...] = (),
) -> SystemContainer:
    """Load configuration once and wire application services explicitly.

    Infrastructure construction remains outside this function. That boundary
    keeps imports of optional hardware libraries out of bootstrap tests and
    makes every external dependency visible at the call site.
    """

    operational = config_repository.load()
    snapshot = SystemSnapshot(operational=operational, identity=identity)
    enabled_ids = {camera.camera_id for camera in operational.cameras if camera.enabled}

    duplicate_ids = _duplicates(binding.camera_id for binding in cameras)
    if duplicate_ids:
        raise ValueError(f"duplicate camera bindings: {', '.join(sorted(duplicate_ids))}")

    unknown_ids = {binding.camera_id for binding in cameras} - enabled_ids
    if unknown_ids:
        raise ValueError(
            f"bindings for disabled or unknown cameras: {', '.join(sorted(unknown_ids))}"
        )

    capture_replays: dict[str, CaptureReplay] = {}
    supervisors: list[CameraSupervisor] = []
    for binding in cameras:
        camera_id = CameraId(binding.camera_id)
        capture_replays[binding.camera_id] = CaptureReplay(
            segments=binding.segments,
            media=binding.media,
        )
        supervisors.append(
            CameraSupervisor(
                camera_id=camera_id,
                process=binding.process,
                segments=binding.segments,
                clock=clock,
                stale_restart_after_seconds=operational.capture.stale_after_seconds,
            )
        )

    process_clip_job = None
    if delivery is not None:
        process_clip_job = ProcessClipJob(
            jobs=delivery.jobs,
            leases=delivery.leases,
            media=delivery.media,
            backend=delivery.backend,
            artifacts=delivery.artifacts,
            retry_policy=delivery.retry_policy,
            clock=delivery.clock,
            owner_id=delivery.owner_id,
            lease_ttl=delivery.lease_ttl,
            dev_mode=operational.processing.development_mode,
        )

    return SystemContainer(
        snapshot=snapshot,
        runtime=EdgeRuntime(lifecycle_components),
        capture_replays=MappingProxyType(capture_replays),
        camera_supervision=CameraSupervisionCoordinator(tuple(supervisors)),
        process_clip_job=process_clip_job,
    )


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates
