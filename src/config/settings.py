from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from src.config.config_loader import (
    OperationalConfig,
    get_effective_config,
)


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
    """Carrega configuração MQTT a partir do loader central + segredos de env.

    Parâmetros operacionais (host, port, tls, keepalive, etc.) vêm do
    config_loader (config.json → env → defaults).
    Credenciais (username, password) sempre de env/secret.
    """
    cfg: OperationalConfig = get_effective_config()
    mqtt = cfg.mqtt

    # client_id: identidade do device — sempre de env, nunca de config.json
    client_id = (
        _env_str("GN_MQTT_CLIENT_ID")
        or _env_str("DEVICE_ID")
        or _env_str("GN_DEVICE_ID")
        or "grava-nois-edge"
    )

    return MQTTConfig(
        enabled=mqtt.enabled,
        host=mqtt.broker.host,
        port=mqtt.broker.port,
        # credenciais: sempre de env/secret
        username=_env_str("GN_MQTT_USERNAME") or None,
        password=_env_str("GN_MQTT_PASSWORD") or None,
        client_id=client_id,
        keepalive=mqtt.keepalive_seconds,
        heartbeat_interval_sec=mqtt.heartbeat_interval_seconds,
        topic_prefix=mqtt.topic_prefix,
        qos=mqtt.qos,
        retain_presence=mqtt.retain_presence,
        use_tls=mqtt.broker.tls,
        # agent_version: de env/deploy, não de config.json
        agent_version=_env_str("GN_AGENT_VERSION", "local-dev"),
    )


def load_capture_configs(base: Path, seg_time: int) -> List[CaptureConfig]:
    """Carrega configurações de câmera a partir do loader central ou env legado.

    Política de fonte de câmeras:
      1. Se config.json possui array 'cameras' não vazio → usa-o (gerenciado)
      2. Caso contrário → fallback para GN_CAMERAS_JSON / GN_RTSP_URLS / GN_RTSP_URL (env legado)
      3. Se nenhuma fonte RTSP → câmera V4L2 local

    URLs RTSP com credenciais devem usar 'env:VAR_NAME' em config.json ou
    permanecer exclusivamente em GN_CAMERAS_JSON / GN_RTSP_URL no env.
    """
    cfg: OperationalConfig = get_effective_config()
    capture = cfg.capture

    # Usa pre/post segments da config operacional como base para fontes RTSP
    pre_seg_cfg = capture.pre_segments
    post_seg_cfg = capture.post_segments
    pre_sec_cfg = pre_seg_cfg * seg_time
    post_sec_cfg = post_seg_cfg * seg_time

    buffer_base = Path(os.getenv("GN_BUFFER_DIR", "/dev/shm/grn_buffer"))

    def _build_rtsp_cfg(
        camera_id: str,
        url: str,
        camera_name: Optional[str],
        use_isolated_dirs: bool,
        pico_trigger_token: Optional[str] = None,
        pre_seg_override: Optional[int] = None,
        post_seg_override: Optional[int] = None,
    ) -> CaptureConfig:
        camera_suffix = Path(camera_id) if use_isolated_dirs else Path()
        _pre_seg = pre_seg_override if pre_seg_override is not None else pre_seg_cfg
        _post_seg = post_seg_override if post_seg_override is not None else post_seg_cfg
        return CaptureConfig(
            camera_id=camera_id,
            camera_name=camera_name,
            source_type="rtsp",
            rtsp_url=url,
            buffer_dir=buffer_base / camera_suffix,
            clips_dir=base / "recorded_clips" / camera_suffix,
            queue_dir=base / "queue_raw" / camera_suffix,
            failed_dir_highlight=base / "failed_clips" / camera_suffix,
            seg_time=seg_time,
            pre_seconds=_pre_seg * seg_time,
            post_seconds=_post_seg * seg_time,
            scan_interval=1,
            max_buffer_seconds=40,
            pre_segments=_pre_seg,
            post_segments=_post_seg,
            pico_trigger_token=pico_trigger_token,
        )

    # --- Fonte 1: cameras de config.json ---
    if cfg.cameras:
        enabled = [c for c in cfg.cameras if c.enabled]
        configs: List[CaptureConfig] = []
        use_isolated_dirs = len(enabled) > 1
        for cam in enabled:
            if cam.source_type == "v4l2":
                configs.append(
                    CaptureConfig(
                        camera_id=cam.id,
                        camera_name=cam.name,
                        source_type="v4l2",
                        buffer_dir=buffer_base,
                        clips_dir=base / "recorded_clips",
                        queue_dir=base / "queue_raw",
                        device=capture.v4l2.device,
                        seg_time=seg_time,
                        pre_seconds=pre_sec_cfg,
                        post_seconds=post_sec_cfg,
                        scan_interval=1,
                        max_buffer_seconds=40,
                        failed_dir_highlight=base / "failed_clips",
                        pre_segments=pre_seg_cfg,
                        post_segments=post_seg_cfg,
                    )
                )
                continue

            rtsp_url = cam.resolve_rtsp_url()
            if not rtsp_url:
                continue
            configs.append(
                _build_rtsp_cfg(
                    camera_id=cam.id,
                    url=rtsp_url,
                    camera_name=cam.name,
                    use_isolated_dirs=use_isolated_dirs,
                    pico_trigger_token=cam.pico_trigger_token,
                    pre_seg_override=cam.pre_segments,
                    post_seg_override=cam.post_segments,
                )
            )
        if configs:
            return configs

    # --- Fonte 2: env legado (GN_CAMERAS_JSON / GN_RTSP_URLS / GN_RTSP_URL) ---
    cameras_json = (os.getenv("GN_CAMERAS_JSON") or "").strip()
    rtsp_urls_csv = (os.getenv("GN_RTSP_URLS") or "").strip()
    rtsp_url_legacy = (os.getenv("GN_RTSP_URL") or "").strip()

    has_any_rtsp_source = bool(cameras_json or rtsp_urls_csv or rtsp_url_legacy)

    if not has_any_rtsp_source:
        # Sem RTSP: ajusta pre/post para V4L2
        pre_sec_cfg = 25
        post_sec_cfg = 10
        pre_seg_cfg = None  # type: ignore[assignment]
        post_seg_cfg = None  # type: ignore[assignment]

    if cameras_json:
        parsed = json.loads(cameras_json)
        if not isinstance(parsed, list):
            raise ValueError("GN_CAMERAS_JSON deve ser uma lista JSON")
        enabled_env = [c for c in parsed if isinstance(c, dict) and c.get("enabled", True)]
        configs = []
        use_isolated_dirs = len(enabled_env) > 1
        for idx, camera in enumerate(enabled_env, start=1):
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

    # --- Fonte 3: V4L2 local (fallback final) ---
    return [
        CaptureConfig(
            camera_id="cam01",
            camera_name="local_device",
            source_type="v4l2",
            buffer_dir=buffer_base,
            clips_dir=base / "recorded_clips",
            queue_dir=base / "queue_raw",
            device=capture.v4l2.device,
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
