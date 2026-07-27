"""Read-only bridge from the legacy loader to immutable domain snapshots."""

from src.config.config_loader import OperationalConfig
from src.domain.configuration import (
    CameraSnapshot,
    CapturePolicy,
    GpioSnapshot,
    MqttSnapshot,
    OperationalConfigSnapshot,
    OperationWindowSnapshot,
    PicoSnapshot,
    ProcessingPolicy,
    RtspSnapshot,
    TriggerSnapshot,
    V4l2Snapshot,
    WatermarkSnapshot,
)


class LegacyOperationalConfigAdapter:
    """Convert a mutable legacy config once, without retaining its children."""

    def __init__(
        self,
        config: OperationalConfig,
        *,
        buffer_seconds: float | None = None,
        stale_after_seconds: float | None = None,
    ) -> None:
        self._snapshot = _to_snapshot(
            config,
            buffer_seconds=buffer_seconds,
            stale_after_seconds=stale_after_seconds,
        )

    def load(self) -> OperationalConfigSnapshot:
        return self._snapshot


def _to_snapshot(
    config: OperationalConfig,
    *,
    buffer_seconds: float | None,
    stale_after_seconds: float | None,
) -> OperationalConfigSnapshot:
    segment_seconds = float(config.capture.segment_seconds)
    pre_seconds = config.capture.pre_segments * segment_seconds
    post_seconds = config.capture.post_segments * segment_seconds
    effective_buffer = (
        float(buffer_seconds)
        if buffer_seconds is not None
        else max(40.0, pre_seconds + post_seconds + 2 * segment_seconds)
    )
    effective_stale = (
        float(stale_after_seconds)
        if stale_after_seconds is not None
        else max(5.0, 3 * segment_seconds)
    )

    return OperationalConfigSnapshot(
        config_version=config.config_version,
        updated_at=config.updated_at,
        capture=CapturePolicy(
            segment_seconds=segment_seconds,
            buffer_seconds=effective_buffer,
            stale_after_seconds=effective_stale,
            pre_segments=config.capture.pre_segments,
            post_segments=config.capture.post_segments,
        ),
        rtsp=RtspSnapshot(**vars(config.capture.rtsp)),
        v4l2=V4l2Snapshot(**vars(config.capture.v4l2)),
        cameras=tuple(
            CameraSnapshot(
                camera_id=camera.id,
                name=camera.name,
                enabled=camera.enabled,
                source_type=camera.source_type,
                rtsp_url_reference=camera.rtsp_url,
                pico_trigger_token=camera.pico_trigger_token,
                pre_segments=camera.pre_segments,
                post_segments=camera.post_segments,
            )
            for camera in config.cameras
        ),
        triggers=TriggerSnapshot(
            source=config.triggers.source,
            max_workers=config.triggers.max_workers,
            pico=PicoSnapshot(**vars(config.triggers.pico)),
            gpio=GpioSnapshot(**vars(config.triggers.gpio)),
        ),
        processing=ProcessingPolicy(
            light_mode=config.processing.light_mode,
            max_attempts=config.processing.max_attempts,
        ),
        watermark=WatermarkSnapshot(**vars(config.processing.watermark)),
        operation_window=OperationWindowSnapshot(**vars(config.operation_window)),
        mqtt=MqttSnapshot(
            enabled=config.mqtt.enabled,
            host=config.mqtt.broker.host,
            port=config.mqtt.broker.port,
            tls=config.mqtt.broker.tls,
            keepalive_seconds=config.mqtt.keepalive_seconds,
            heartbeat_interval_seconds=config.mqtt.heartbeat_interval_seconds,
            topic_prefix=config.mqtt.topic_prefix,
            qos=config.mqtt.qos,
            retain_presence=config.mqtt.retain_presence,
        ),
    )
