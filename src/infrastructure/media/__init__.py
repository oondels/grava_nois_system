"""Media adapters."""

from .capture_command import FfmpegCaptureCommandBuilder
from .capture_process import SubprocessCaptureProcess
from .legacy_adapter import (
    ConfiguredLegacyMediaToolAdapter,
    LegacyMediaToolAdapter,
    WatermarkPolicy,
)
from .segment_buffer_repository import SegmentBufferRepository

__all__ = [
    "ConfiguredLegacyMediaToolAdapter",
    "FfmpegCaptureCommandBuilder",
    "LegacyMediaToolAdapter",
    "SegmentBufferRepository",
    "SubprocessCaptureProcess",
    "WatermarkPolicy",
]
