from __future__ import annotations

from dotenv import load_dotenv

from src.config.settings import CaptureConfig
from src.video.buffer import SegmentBuffer
from src.video.capture import check_rtsp_connectivity, start_ffmpeg, _calc_start_number
from src.video.processor import (
    build_highlight,
    WatermarkSpec,
    ffprobe_metadata,
    enqueue_clip,
    add_image_watermark,
    _sha256_file,
    generate_thumbnail,
)

load_dotenv()

__all__ = [
    "CaptureConfig",
    "check_rtsp_connectivity",
    "_calc_start_number",
    "start_ffmpeg",
    "SegmentBuffer",
    "build_highlight",
    "WatermarkSpec",
    "ffprobe_metadata",
    "enqueue_clip",
    "add_image_watermark",
    "_sha256_file",
    "generate_thumbnail",
]
