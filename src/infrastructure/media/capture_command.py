"""Deterministic FFmpeg capture command construction."""

from __future__ import annotations

from dataclasses import dataclass

from src.config.settings import CaptureConfig
from src.domain.configuration import ProcessingPolicy, RtspSnapshot, V4l2Snapshot


@dataclass(frozen=True, slots=True)
class FfmpegCaptureCommandBuilder:
    """Build capture commands exclusively from resolved configuration values."""

    executable: str = "ffmpeg"

    def build(
        self,
        *,
        capture: CaptureConfig,
        rtsp: RtspSnapshot,
        v4l2: V4l2Snapshot,
        processing: ProcessingPolicy,
        segment_start_number: int,
    ) -> tuple[str, ...]:
        if not self.executable.strip():
            raise ValueError("ffmpeg executable must not be empty")
        if capture.seg_time <= 0:
            raise ValueError("segment duration must be positive")
        if segment_start_number < 0:
            raise ValueError("segment_start_number must not be negative")

        output = str(capture.buffer_dir / "buffer%06d.ts")
        if capture.source_type == "rtsp":
            if not capture.rtsp_url or not capture.rtsp_url.strip():
                raise ValueError(f"RTSP URL missing for camera {capture.camera_id}")
            return self._rtsp(
                capture=capture,
                rtsp=rtsp,
                processing=processing,
                segment_start_number=segment_start_number,
                output=output,
            )
        if capture.source_type == "v4l2":
            return self._v4l2(
                capture=capture,
                v4l2=v4l2,
                segment_start_number=segment_start_number,
                output=output,
            )
        raise ValueError(f"unsupported capture source: {capture.source_type}")

    def _rtsp(
        self,
        *,
        capture: CaptureConfig,
        rtsp: RtspSnapshot,
        processing: ProcessingPolicy,
        segment_start_number: int,
        output: str,
    ) -> tuple[str, ...]:
        profile = rtsp.profile or ("compatible" if processing.light_mode else "hq")
        reencode = (profile == "compatible") if rtsp.reencode is None else rtsp.reencode
        command = [
            self.executable,
            "-nostdin",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
        ]
        if rtsp.use_wallclock_timestamps:
            command.extend(("-use_wallclock_as_timestamps", "1"))
        if rtsp.low_latency_input:
            command.extend(("-fflags", "nobuffer"))
        command.extend(
            (
                "-fflags",
                "+genpts",
                "-err_detect",
                "ignore_err",
                "-i",
                capture.rtsp_url or "",
                "-map",
                "0:v:0",
                "-an",
            )
        )
        if reencode:
            if rtsp.fps:
                command.extend(("-vf", f"fps={rtsp.fps}"))
            if rtsp.low_delay_codec_flags:
                command.extend(("-flags", "low_delay"))
            command.extend(
                (
                    "-c:v",
                    "libx264",
                    "-preset",
                    rtsp.preset or "veryfast",
                    "-crf",
                    str(rtsp.crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    str(rtsp.gop),
                    "-keyint_min",
                    str(rtsp.gop),
                    "-sc_threshold",
                    "0",
                    "-force_key_frames",
                    f"expr:gte(t,n_forced*{capture.seg_time})",
                    "-fps_mode",
                    "vfr",
                )
            )
        else:
            command.extend(("-c:v", "copy"))
        self._append_segment_output(
            command,
            capture=capture,
            segment_start_number=segment_start_number,
            reset_timestamps="1",
            output=output,
        )
        return tuple(command)

    def _v4l2(
        self,
        *,
        capture: CaptureConfig,
        v4l2: V4l2Snapshot,
        segment_start_number: int,
        output: str,
    ) -> tuple[str, ...]:
        framerate = str(v4l2.framerate)
        gop = max(1, int(float(framerate)))
        command = [
            self.executable,
            "-nostdin",
            "-f",
            "v4l2",
            "-thread_queue_size",
            "512",
            "-input_format",
            "mjpeg",
            "-framerate",
            framerate,
            "-video_size",
            v4l2.video_size,
            "-i",
            capture.device or v4l2.device,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-r",
            framerate,
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{capture.seg_time})",
        ]
        self._append_segment_output(
            command,
            capture=capture,
            segment_start_number=segment_start_number,
            reset_timestamps="0",
            output=output,
        )
        return tuple(command)

    @staticmethod
    def _append_segment_output(
        command: list[str],
        *,
        capture: CaptureConfig,
        segment_start_number: int,
        reset_timestamps: str,
        output: str,
    ) -> None:
        command.extend(
            (
                "-f",
                "segment",
                "-segment_format",
                "mpegts",
                "-segment_time",
                str(capture.seg_time),
                "-segment_start_number",
                str(segment_start_number),
                "-reset_timestamps",
                reset_timestamps,
                output,
            )
        )
