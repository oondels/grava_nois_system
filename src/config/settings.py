from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse


@dataclass
class CaptureConfig:
    camera_id: str
    buffer_dir: Path
    clips_dir: Path  # onde o highlight nasce
    queue_dir: Path  # fila para tratamento posterior (raw)
    failed_dir_highlight: Path
    source_type: str = "rtsp"
    camera_name: Optional[str] = None
    rtsp_url: Optional[str] = None
    device: str = "/dev/video0"
    seg_time: int = 1
    pre_seconds: int = 25
    post_seconds: int = 10
    scan_interval: float = 1
    max_buffer_seconds: int = 40
    pre_segments: Optional[int] = None
    post_segments: Optional[int] = None
    pico_trigger_token: Optional[str] = None

    @property
    def max_segments(self) -> int:
        return max(1, int(self.max_buffer_seconds / self.seg_time))

    def ensure_dirs(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir_highlight.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class MQTTConfig:
    enabled: bool
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    client_id: str
    keepalive: int
    heartbeat_interval_sec: int
    topic_prefix: str
    qos: int
    retain_presence: bool
    use_tls: bool
    agent_version: str

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.host)

    def topic_for(self, device_id: str, suffix: str) -> str:
        normalized_device_id = _validate_mqtt_device_id(device_id)
        normalized_suffix = _validate_mqtt_topic_suffix(suffix)
        base = self.topic_prefix.strip("/") or "grn"
        return f"{base}/devices/{normalized_device_id}/{normalized_suffix}"


def _validate_mqtt_device_id(device_id: str) -> str:
    normalized = str(device_id or "").strip()
    if not normalized:
        raise ValueError("device_id MQTT nao pode ser vazio")
    if any(char in normalized for char in ("/", "+", "#", "\x00")):
        raise ValueError("device_id MQTT contem caracteres invalidos para topico")
    return normalized


def _validate_mqtt_topic_suffix(suffix: str) -> str:
    normalized = str(suffix or "").strip("/")
    if not normalized:
        raise ValueError("sufixo MQTT nao pode ser vazio")
    if any(char in normalized for char in ("+", "#", "\x00")):
        raise ValueError("sufixo MQTT contem caracteres invalidos para topico")
    return normalized


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(1, int(float(value)))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _parse_mqtt_host_and_port(
    broker_url: str,
    fallback_port: int,
) -> tuple[str, int, bool]:
    raw_value = broker_url.strip()
    if not raw_value:
        return "", fallback_port, False

    if "://" not in raw_value:
        if ":" in raw_value and raw_value.count(":") == 1:
            host, raw_port = raw_value.split(":", 1)
            try:
                return host.strip(), max(1, int(raw_port)), False
            except ValueError:
                return host.strip(), fallback_port, False
        return raw_value, fallback_port, False

    parsed = urlparse(raw_value)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    port = parsed.port or fallback_port
    use_tls = scheme in {"mqtts", "ssl", "tls"}
    return host, port, use_tls


def load_mqtt_config() -> MQTTConfig:
    enabled = _env_bool("GN_MQTT_ENABLED", False)
    broker_url = _env_str("GN_MQTT_BROKER_URL") or _env_str("GN_MQTT_HOST")
    default_port = _env_int("GN_MQTT_PORT", 1883)
    host, port_from_url, tls_from_url = _parse_mqtt_host_and_port(
        broker_url,
        default_port,
    )
    port = _env_int("GN_MQTT_PORT", port_from_url)
    use_tls = _env_bool("GN_MQTT_TLS", tls_from_url)
    client_id = (
        _env_str("GN_MQTT_CLIENT_ID")
        or _env_str("DEVICE_ID")
        or _env_str("GN_DEVICE_ID")
        or "grava-nois-edge"
    )

    return MQTTConfig(
        enabled=enabled,
        host=host,
        port=port,
        username=_env_str("GN_MQTT_USERNAME") or None,
        password=_env_str("GN_MQTT_PASSWORD") or None,
        client_id=client_id,
        keepalive=_env_int("GN_MQTT_KEEPALIVE", 60),
        heartbeat_interval_sec=_env_int("GN_MQTT_HEARTBEAT_INTERVAL_SEC", 30),
        topic_prefix=_env_str("GN_MQTT_TOPIC_PREFIX", "grn"),
        qos=max(0, min(2, _env_int("GN_MQTT_QOS", 1))),
        retain_presence=_env_bool("GN_MQTT_RETAIN_PRESENCE", True),
        use_tls=use_tls,
        agent_version=_env_str("GN_AGENT_VERSION", "local-dev"),
    )


def load_capture_configs(base: Path, seg_time: int) -> List[CaptureConfig]:
    cameras_json = (os.getenv("GN_CAMERAS_JSON") or "").strip()
    rtsp_urls_csv = (os.getenv("GN_RTSP_URLS") or "").strip()
    rtsp_url_legacy = (os.getenv("GN_RTSP_URL") or "").strip()

    has_any_rtsp_source = bool(cameras_json or rtsp_urls_csv or rtsp_url_legacy)

    if has_any_rtsp_source:
        pre_seg_cfg = _env_int("GN_RTSP_PRE_SEGMENTS", 6)
        post_seg_cfg = _env_int("GN_RTSP_POST_SEGMENTS", 3)
        pre_sec_cfg = pre_seg_cfg * seg_time
        post_sec_cfg = post_seg_cfg * seg_time
    else:
        pre_seg_cfg = None
        post_seg_cfg = None
        pre_sec_cfg = 25
        post_sec_cfg = 10

    def _build_rtsp_cfg(
        camera_id: str,
        url: str,
        camera_name: Optional[str],
        use_isolated_dirs: bool,
        pico_trigger_token: Optional[str] = None,
    ) -> CaptureConfig:
        camera_suffix = Path(camera_id) if use_isolated_dirs else Path()
        return CaptureConfig(
            camera_id=camera_id,
            camera_name=camera_name,
            source_type="rtsp",
            rtsp_url=url,
            buffer_dir=Path(os.getenv("GN_BUFFER_DIR", "/dev/shm/grn_buffer"))
            / camera_suffix,
            clips_dir=base / "recorded_clips" / camera_suffix,
            queue_dir=base / "queue_raw" / camera_suffix,
            failed_dir_highlight=base / "failed_clips" / camera_suffix,
            seg_time=seg_time,
            pre_seconds=pre_sec_cfg,
            post_seconds=post_sec_cfg,
            scan_interval=1,
            max_buffer_seconds=40,
            pre_segments=pre_seg_cfg,
            post_segments=post_seg_cfg,
            pico_trigger_token=pico_trigger_token,
        )

    if cameras_json:
        parsed = json.loads(cameras_json)
        if not isinstance(parsed, list):
            raise ValueError("GN_CAMERAS_JSON deve ser uma lista JSON")
        enabled = [c for c in parsed if isinstance(c, dict) and c.get("enabled", True)]
        configs: List[CaptureConfig] = []
        use_isolated_dirs = len(enabled) > 1
        for idx, camera in enumerate(enabled, start=1):
            rtsp_url = str(camera.get("rtsp_url") or "").strip()
            if not rtsp_url:
                continue
            camera_id = str(camera.get("id") or f"cam{idx:02d}").strip() or f"cam{idx:02d}"
            camera_name = camera.get("name")
            raw_token = camera.get("pico_trigger_token")
            pico_token = str(raw_token).strip() if raw_token else None
            configs.append(
                _build_rtsp_cfg(
                    camera_id=camera_id,
                    url=rtsp_url,
                    camera_name=str(camera_name) if camera_name is not None else None,
                    use_isolated_dirs=use_isolated_dirs,
                    pico_trigger_token=pico_token,
                )
            )
        if configs:
            return configs

    if rtsp_urls_csv:
        urls = [u.strip() for u in rtsp_urls_csv.split(",") if u.strip()]
        use_isolated_dirs = len(urls) > 1
        configs = [
            _build_rtsp_cfg(
                camera_id=f"cam{idx:02d}",
                url=url,
                camera_name=None,
                use_isolated_dirs=use_isolated_dirs,
            )
            for idx, url in enumerate(urls, start=1)
        ]
        if configs:
            return configs

    if rtsp_url_legacy:
        return [
            _build_rtsp_cfg(
                camera_id="cam01",
                url=rtsp_url_legacy,
                camera_name=None,
                use_isolated_dirs=False,
            )
        ]

    return [
        CaptureConfig(
            camera_id="cam01",
            camera_name="local_device",
            source_type="v4l2",
            buffer_dir=Path(os.getenv("GN_BUFFER_DIR", "/dev/shm/grn_buffer")),
            clips_dir=base / "recorded_clips",
            queue_dir=base / "queue_raw",
            device="/dev/video0",
            seg_time=seg_time,
            pre_seconds=pre_sec_cfg,
            post_seconds=post_sec_cfg,
            scan_interval=1,
            max_buffer_seconds=40,
            failed_dir_highlight=base / "failed_clips",
            pre_segments=pre_seg_cfg,
            post_segments=post_seg_cfg,
        )
    ]
