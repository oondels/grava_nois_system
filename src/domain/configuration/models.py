"""Immutable configuration values consumed by the new architecture."""

from dataclasses import dataclass

from src.domain.exceptions import InvariantViolation


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    segment_seconds: float
    buffer_seconds: float
    stale_after_seconds: float
    pre_segments: int = 6
    post_segments: int = 3

    def __post_init__(self) -> None:
        if min(self.segment_seconds, self.buffer_seconds, self.stale_after_seconds) <= 0:
            raise InvariantViolation("capture policy durations must be positive")
        if self.buffer_seconds < self.segment_seconds:
            raise InvariantViolation("buffer must hold at least one segment")
        if self.pre_segments < 1 or self.post_segments < 1:
            raise InvariantViolation("capture segment windows must be positive")


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    light_mode: bool = False
    development_mode: bool = False
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InvariantViolation("max attempts must be at least one")


@dataclass(frozen=True, slots=True)
class RtspSnapshot:
    max_retries: int
    timeout_seconds: int
    startup_check_seconds: float
    reencode: bool | None
    fps: str
    gop: int
    preset: str
    crf: int
    use_wallclock_timestamps: bool
    profile: str | None
    low_latency_input: bool
    low_delay_codec_flags: bool


@dataclass(frozen=True, slots=True)
class V4l2Snapshot:
    device: str
    framerate: int
    video_size: str


@dataclass(frozen=True, slots=True)
class CameraSnapshot:
    camera_id: str
    name: str | None
    enabled: bool
    source_type: str
    rtsp_url_reference: str | None
    pico_trigger_token: str | None
    pre_segments: int | None
    post_segments: int | None


@dataclass(frozen=True, slots=True)
class PicoSnapshot:
    port: str | None
    global_token: str


@dataclass(frozen=True, slots=True)
class GpioSnapshot:
    pin: int | None
    debounce_ms: float
    cooldown_seconds: float


@dataclass(frozen=True, slots=True)
class TriggerSnapshot:
    source: str
    max_workers: int | None
    pico: PicoSnapshot
    gpio: GpioSnapshot


@dataclass(frozen=True, slots=True)
class WatermarkSnapshot:
    relative_width: float
    opacity: float
    margin: int


@dataclass(frozen=True, slots=True)
class OperationWindowSnapshot:
    time_zone: str
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class MqttSnapshot:
    enabled: bool
    host: str
    port: int
    tls: bool
    keepalive_seconds: int
    heartbeat_interval_seconds: int
    topic_prefix: str
    qos: int
    retain_presence: bool


@dataclass(frozen=True, slots=True)
class OperationalConfigSnapshot:
    """Complete non-secret operational configuration at one point in time."""

    config_version: int
    updated_at: str | None
    capture: CapturePolicy
    rtsp: RtspSnapshot
    v4l2: V4l2Snapshot
    cameras: tuple[CameraSnapshot, ...]
    triggers: TriggerSnapshot
    processing: ProcessingPolicy
    watermark: WatermarkSnapshot
    operation_window: OperationWindowSnapshot
    mqtt: MqttSnapshot
