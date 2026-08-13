from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

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
      1. Se config.json possui o campo 'cameras' → usa-o como fonte autoritativa
      2. Se o campo estiver ausente → fallback para env legado
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
    default_buffer_seconds = max(
        40,
        pre_sec_cfg + post_sec_cfg + (2 * seg_time),
    )

    def _buffer_seconds_for(pre_segments: int, post_segments: int) -> int:
        required = (pre_segments + post_segments + 2) * seg_time
        effective = (
            capture.buffer_seconds
            if capture.buffer_seconds is not None
            else max(default_buffer_seconds, required)
        )
        if effective < required:
            raise ValueError(
                "capture.bufferSeconds/GN_MAX_BUFFER_SECONDS deve ser >= "
                f"{required}s para comportar a janela da câmera "
                f"({pre_segments} pre + {post_segments} post + 2 segmentos de margem)"
            )
        return effective

    def _buffer_seconds_for_window(pre_seconds: int, post_seconds: int) -> int:
        required = pre_seconds + post_seconds + (2 * seg_time)
        effective = capture.buffer_seconds or max(40, required)
        if effective < required:
            raise ValueError(
                "capture.bufferSeconds/GN_MAX_BUFFER_SECONDS deve ser >= "
                f"{required}s para comportar a janela V4L2 e 2 segmentos de margem"
            )
        return effective

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
            max_buffer_seconds=_buffer_seconds_for(_pre_seg, _post_seg),
            pre_segments=_pre_seg,
            post_segments=_post_seg,
            pico_trigger_token=pico_trigger_token,
        )

    # --- Fonte 1: cameras de config.json ---
    if cfg.cameras_managed:
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
                        max_buffer_seconds=_buffer_seconds_for(pre_seg_cfg, post_seg_cfg),
                        failed_dir_highlight=base / "failed_clips",
                        pre_segments=pre_seg_cfg,
                        post_segments=post_seg_cfg,
                    )
                )
                continue

            try:
                rtsp_url = cam.resolve_rtsp_url()
            except ValueError as exc:
                raise ValueError(
                    f"camera gerenciada {cam.id!r} possui rtspUrl invalida: {exc}"
                ) from exc
            if not rtsp_url:
                raise ValueError(
                    f"camera gerenciada {cam.id!r} exige rtspUrl ou referencia env:VAR_NAME"
                )
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
            max_buffer_seconds=_buffer_seconds_for_window(pre_sec_cfg, post_sec_cfg),
            failed_dir_highlight=base / "failed_clips",
            pre_segments=pre_seg_cfg,
            post_segments=post_seg_cfg,
        )
    ]
