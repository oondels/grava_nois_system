"""Loader central de configuração operacional do grava_nois_system.

Política de precedência para parâmetros operacionais (não sensíveis):
  1. Defaults seguros (valores embutidos nos dataclasses)
  2. Variáveis de ambiente — fallback legado quando config.json ausente
  3. config.json — vence sobre env para parâmetros operacionais quando presente

Parâmetros que NUNCA saem de env/secret (fora desta camada):
  - DEVICE_SECRET / GN_DEVICE_SECRET
  - GN_API_TOKEN / API_TOKEN
  - GN_MQTT_PASSWORD
  - GN_MQTT_USERNAME
  - DEVICE_ID / GN_DEVICE_ID
  - GN_CLIENT_ID / CLIENT_ID
  - GN_VENUE_ID / VENUE_ID
  - GN_API_BASE / API_BASE_URL
  - Flags de dev/teste: DEV, DEV_VIDEO_MODE, GN_HMAC_DRY_RUN, GN_FORCE_RASPBERRY_PI
  - Paths de host/container: GN_BUFFER_DIR, GN_LOG_DIR

Parâmetros que dependem de decisão arquitetural e permanecem em env nesta fase:
  - GN_PICO_PORT (path de device no host)
  - GN_RTSP_URL / GN_RTSP_URLS / GN_CAMERAS_JSON (URLs RTSP com credenciais embutidas)
  - GN_MQTT_BROKER_URL / GN_MQTT_HOST (mantém suporte legado; broker.host pode vir de config.json)
  - GN_MQTT_CLIENT_ID (identidade derivada de DEVICE_ID)

Campos que suportam hot-reload futuro (sem restart do pipeline):
  - operationWindow.* (fuso, início e fim da janela)
  - triggers.gpio.cooldownSeconds e debounceMs
  - mqtt.heartbeatIntervalSeconds
  - processing.watermark.*

Campos que exigem restart/reload controlado:
  - cameras (estrutura e source)
  - capture.segmentSeconds
  - capture.rtsp.* (source RTSP)
  - triggers.source
  - triggers.gpio.pin

Uso:
    from src.config.config_loader import get_effective_config

    cfg = get_effective_config()
    seg_time = cfg.capture.segment_seconds
    tz = cfg.operation_window.time_zone
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from src.config.config_schema import ConfigValidationError, validate_config_dict

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Localização padrão do config.json
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"


def _default_config_path() -> Path:
    """Retorna o path do config.json, respeitando GN_CONFIG_PATH se definido."""
    env_path = os.getenv("GN_CONFIG_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return _DEFAULT_CONFIG_PATH


def get_config_path() -> Path:
    """Retorna o path efetivo do config.json sem carregar a configuração."""
    return _default_config_path()


# ---------------------------------------------------------------------------
# Dataclasses de configuração operacional
# ---------------------------------------------------------------------------


@dataclass
class RtspConfig:
    """Parâmetros de captura RTSP."""

    max_retries: int = 10
    timeout_seconds: int = 5
    startup_check_seconds: float = 1.0
    reencode: bool = True
    # fps: string vazia = sem filtro fps; ex: "25" aplica -vf fps=25
    fps: str = ""
    gop: int = 25
    preset: str = "veryfast"
    crf: int = 23
    use_wallclock_timestamps: bool = False


@dataclass
class V4l2Config:
    """Parâmetros de captura V4L2 (câmera local)."""

    device: str = "/dev/video0"
    framerate: int = 30
    video_size: str = "1280x720"


@dataclass
class CaptureParams:
    """Configuração geral de captura e segmentação."""

    segment_seconds: int = 1
    # pre/post_segments usados quando há fonte RTSP detectada
    pre_segments: int = 6
    post_segments: int = 3
    rtsp: RtspConfig = field(default_factory=RtspConfig)
    v4l2: V4l2Config = field(default_factory=V4l2Config)


@dataclass
class CameraConfig:
    """Entrada de câmera gerenciada pelo config.json.

    rtsp_url pode ser:
    - URL literal sem credenciais (ex: 'rtsp://192.168.1.10:554/stream')
    - 'env:VAR_NAME' para ler URL de variável de ambiente (para câmeras com credenciais)
    - None quando sourceType='v4l2'

    Credenciais RTSP nunca devem aparecer em texto plano no config.json.
    """

    id: str
    name: Optional[str] = None
    enabled: bool = True
    source_type: str = "rtsp"
    rtsp_url: Optional[str] = None
    pico_trigger_token: Optional[str] = None
    pre_segments: Optional[int] = None
    post_segments: Optional[int] = None

    def resolve_rtsp_url(self) -> Optional[str]:
        """Resolve rtsp_url, expandindo referências 'env:VAR_NAME'."""
        return _resolve_env_ref(self.rtsp_url)


@dataclass
class PicoConfig:
    """Configuração do trigger via Pico serial."""

    # port: None = auto-detect via /dev/serial/by-id
    # Nota: GN_PICO_PORT (path de device) permanece como env nesta fase.
    # Este campo ficará para eventual config avançada por device no futuro.
    port: Optional[str] = None
    global_token: str = "BTN_REPLAY"


@dataclass
class GpioConfig:
    """Configuração do trigger via GPIO (pigpio)."""

    pin: Optional[int] = None
    debounce_ms: float = 300.0
    cooldown_seconds: float = 120.0


@dataclass
class TriggerConfig:
    """Configuração de fontes de trigger físico."""

    # Valores válidos: auto, gpio, pico, both
    source: str = "auto"
    # None = usar número de câmeras ativas como padrão
    max_workers: Optional[int] = None
    pico: PicoConfig = field(default_factory=PicoConfig)
    gpio: GpioConfig = field(default_factory=GpioConfig)


@dataclass
class WatermarkConfig:
    """Parâmetros de watermark/branding."""

    relative_width: float = 0.18
    opacity: float = 0.8
    margin: int = 24


@dataclass
class ProcessingConfig:
    """Configuração de processamento de clips.

    Modos de qualidade:
    - light_mode=False (padrão): alta qualidade — hq_crf + hq_preset.
    - light_mode=True: modo leve para hardware fraco — lm_crf + lm_preset.
    Watermark é sempre aplicada em ambos os modos.
    vertical_format é apenas reframe 9:16 (crop), sem scale forçado.
    """

    light_mode: bool = False
    max_attempts: int = 3
    vertical_format: bool = False
    # Encode de alta qualidade (light_mode=False)
    hq_crf: int = 18
    hq_preset: str = "medium"
    # Encode leve (light_mode=True)
    lm_crf: int = 26
    lm_preset: str = "veryfast"
    watermark: WatermarkConfig = field(default_factory=WatermarkConfig)


@dataclass
class OperationWindowConfig:
    """Janela operacional diária."""

    time_zone: str = "America/Sao_Paulo"
    start: str = "07:00"
    end: str = "23:30"


@dataclass
class MqttBrokerConfig:
    """Endereço do broker MQTT (sem credenciais)."""

    host: str = ""
    port: int = 1883
    tls: bool = False


@dataclass
class MqttParams:
    """Configuração MQTT não sensível."""

    enabled: bool = False
    broker: MqttBrokerConfig = field(default_factory=MqttBrokerConfig)
    keepalive_seconds: int = 60
    heartbeat_interval_seconds: int = 30
    topic_prefix: str = "grn"
    qos: int = 1
    retain_presence: bool = True


@dataclass
class OperationalConfig:
    """Configuração operacional completa (sem segredos, sem identidade de device)."""

    config_version: int = 1
    # updated_at: preenchido pelo loader quando carregado de config.json
    updated_at: Optional[str] = None
    capture: CaptureParams = field(default_factory=CaptureParams)
    # cameras: lista de câmeras de config.json; vazia = usar fontes legadas de env
    cameras: list[CameraConfig] = field(default_factory=list)
    triggers: TriggerConfig = field(default_factory=TriggerConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    operation_window: OperationWindowConfig = field(default_factory=OperationWindowConfig)
    mqtt: MqttParams = field(default_factory=MqttParams)


# ---------------------------------------------------------------------------
# Resolução de referências env:VAR_NAME
# ---------------------------------------------------------------------------


def _resolve_env_ref(value: Optional[str]) -> Optional[str]:
    """Resolve 'env:VAR_NAME' para o valor da variável de ambiente.

    Formatos suportados:
      'env:GN_CAM01_RTSP_URL' → os.getenv('GN_CAM01_RTSP_URL')
      'secretRef:...'        → None (não resolvido nesta fase; use env: como alternativa)
      qualquer outro valor   → retornado como está (ou None se vazio)
    """
    if not value:
        return None
    stripped = value.strip()
    if stripped.startswith("env:"):
        var_name = stripped[4:].strip()
        return (os.getenv(var_name) or "").strip() or None
    if stripped.startswith("secretRef:"):
        _logger.warning(
            "secretRef não resolvido nesta fase: %r. "
            "Use 'env:VAR_NAME' para referenciar segredos via variável de ambiente.",
            stripped,
        )
        return None
    return stripped or None


# ---------------------------------------------------------------------------
# Helpers de leitura de env
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return max(1, int(float(v)))
    except Exception:
        return default


def _env_int_nullable(name: str) -> Optional[int]:
    """Retorna int ou None se env não definido."""
    v = os.getenv(name)
    if v is None:
        return None
    try:
        return max(1, int(float(v)))
    except Exception:
        return None


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip()


def _parse_mqtt_host_and_port(
    broker_url: str, fallback_port: int
) -> tuple[str, int, bool]:
    """Extrai host, port e flag TLS de uma URL ou string host:port."""
    raw = broker_url.strip()
    if not raw:
        return "", fallback_port, False

    if "://" not in raw:
        if ":" in raw and raw.count(":") == 1:
            host, raw_port = raw.split(":", 1)
            try:
                return host.strip(), max(1, int(raw_port)), False
            except ValueError:
                return host.strip(), fallback_port, False
        return raw, fallback_port, False

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    port = parsed.port or fallback_port
    use_tls = scheme in {"mqtts", "ssl", "tls"}
    return host, port, use_tls


# ---------------------------------------------------------------------------
# Construção da config a partir de env (fallback legado)
# ---------------------------------------------------------------------------


def _build_from_env() -> OperationalConfig:
    """Constrói OperationalConfig lendo variáveis de ambiente (fallback legado).

    Esta função preserva 100% de compatibilidade com instalações que não
    possuem config.json. Os valores lidos aqui serão sobrepostos por
    config.json quando ele estiver presente.

    Parâmetros de identidade, segredos e flags de dev NÃO são lidos aqui —
    esses permanecem exclusivamente em env e são consumidos diretamente pelos
    módulos que os precisam (api_client, main.py, etc.).
    """
    # MQTT broker
    broker_url = _env_str("GN_MQTT_BROKER_URL") or _env_str("GN_MQTT_HOST")
    default_port = _env_int("GN_MQTT_PORT", 1883)
    host, port_from_url, tls_from_url = _parse_mqtt_host_and_port(broker_url, default_port)
    mqtt_port = _env_int("GN_MQTT_PORT", port_from_url)
    mqtt_tls = _env_bool("GN_MQTT_TLS", tls_from_url)

    # GPIO pin (int ou None)
    gpio_pin_raw = os.getenv("GN_GPIO_PIN") or os.getenv("GPIO_PIN")
    gpio_pin: Optional[int] = None
    if gpio_pin_raw:
        try:
            gpio_pin = int(gpio_pin_raw.strip())
        except ValueError:
            pass

    return OperationalConfig(
        capture=CaptureParams(
            segment_seconds=_env_int("GN_SEG_TIME", 1),
            pre_segments=_env_int("GN_RTSP_PRE_SEGMENTS", 6),
            post_segments=_env_int("GN_RTSP_POST_SEGMENTS", 3),
            rtsp=RtspConfig(
                max_retries=_env_int("GN_RTSP_MAX_RETRIES", 10),
                timeout_seconds=_env_int("GN_RTSP_TIMEOUT", 5),
                startup_check_seconds=max(
                    0.1, _env_float("GN_FFMPEG_STARTUP_CHECK_SEC", 1.0)
                ),
                reencode=_env_bool("GN_RTSP_REENCODE", True),
                fps=_env_str("GN_RTSP_FPS", ""),
                gop=max(1, _env_int("GN_RTSP_GOP", 25)),
                preset=_env_str("GN_RTSP_PRESET", "veryfast") or "veryfast",
                crf=max(0, min(51, _env_int("GN_RTSP_CRF", 23))),
                use_wallclock_timestamps=_env_bool("GN_RTSP_USE_WALLCLOCK", False),
            ),
            v4l2=V4l2Config(
                device="/dev/video0",
                framerate=_env_int("GN_INPUT_FRAMERATE", 30),
                video_size=_env_str("GN_VIDEO_SIZE", "1280x720") or "1280x720",
            ),
        ),
        # cameras vazia = load_capture_configs() usará GN_CAMERAS_JSON/GN_RTSP_URLS/GN_RTSP_URL
        cameras=[],
        triggers=TriggerConfig(
            source=(_env_str("GN_TRIGGER_SOURCE", "auto") or "auto").lower(),
            max_workers=_env_int_nullable("GN_TRIGGER_MAX_WORKERS"),
            pico=PicoConfig(
                # GN_PICO_PORT permanece em env; get_pico_serial_port() o lê diretamente
                port=None,
                global_token=_env_str("GN_PICO_TRIGGER_TOKEN", "BTN_REPLAY") or "BTN_REPLAY",
            ),
            gpio=GpioConfig(
                pin=gpio_pin,
                debounce_ms=_env_float("GN_GPIO_DEBOUNCE_MS", 300.0),
                cooldown_seconds=_env_float("GN_GPIO_COOLDOWN_SEC", 120.0),
            ),
        ),
        processing=ProcessingConfig(
            light_mode=_env_bool("GN_LIGHT_MODE", False),
            max_attempts=max(1, _env_int("GN_MAX_ATTEMPTS", 3)),
            vertical_format=_env_bool("VERTICAL_FORMAT", False),
            hq_crf=max(0, min(51, _env_int("GN_HQ_CRF", 18))),
            hq_preset=_env_str("GN_HQ_PRESET", "medium") or "medium",
            lm_crf=max(0, min(51, _env_int("GN_LM_CRF", 26))),
            lm_preset=_env_str("GN_LM_PRESET", "veryfast") or "veryfast",
            watermark=WatermarkConfig(
                relative_width=max(0.01, _env_float("GN_WM_REL_WIDTH", 0.18)),
                opacity=max(0.0, min(1.0, _env_float("GN_WM_OPACITY", 0.8))),
                margin=max(0, _env_int("GN_WM_MARGIN", 24)),
            ),
        ),
        operation_window=OperationWindowConfig(
            time_zone=_env_str("GN_TIME_ZONE", "America/Sao_Paulo") or "America/Sao_Paulo",
            start=_env_str("GN_START_TIME", "07:00") or "07:00",
            end=_env_str("GN_END_TIME", "23:30") or "23:30",
        ),
        mqtt=MqttParams(
            enabled=_env_bool("GN_MQTT_ENABLED", False),
            broker=MqttBrokerConfig(host=host, port=mqtt_port, tls=mqtt_tls),
            keepalive_seconds=_env_int("GN_MQTT_KEEPALIVE", 60),
            heartbeat_interval_seconds=_env_int("GN_MQTT_HEARTBEAT_INTERVAL_SEC", 30),
            topic_prefix=_env_str("GN_MQTT_TOPIC_PREFIX", "grn") or "grn",
            qos=max(0, min(2, _env_int("GN_MQTT_QOS", 1))),
            retain_presence=_env_bool("GN_MQTT_RETAIN_PRESENCE", True),
        ),
    )


# ---------------------------------------------------------------------------
# Merge de config.json sobre a base de env
# ---------------------------------------------------------------------------


def _apply_json(base: OperationalConfig, data: dict[str, Any]) -> OperationalConfig:
    """Aplica os campos presentes em data (config.json) sobre a base.

    Apenas campos explicitamente presentes no JSON sobrescrevem o valor base.
    Campos ausentes ou null no JSON mantêm o valor da base (env/default).
    """

    def _get(d: dict, key: str, fallback: Any) -> Any:
        return d[key] if key in d and d[key] is not None else fallback

    # capture
    cap_d = data.get("capture") or {}
    rtsp_d = cap_d.get("rtsp") or {}
    v4l2_d = cap_d.get("v4l2") or {}

    # fps pode ser int, float ou str no JSON
    fps_raw = rtsp_d.get("fps")
    fps_str: str = base.capture.rtsp.fps
    if fps_raw is not None:
        fps_str = str(fps_raw).strip() if fps_raw != "" else ""

    rtsp = RtspConfig(
        max_retries=_get(rtsp_d, "maxRetries", base.capture.rtsp.max_retries),
        timeout_seconds=_get(rtsp_d, "timeoutSeconds", base.capture.rtsp.timeout_seconds),
        startup_check_seconds=max(
            0.1, _get(rtsp_d, "startupCheckSeconds", base.capture.rtsp.startup_check_seconds)
        ),
        reencode=_get(rtsp_d, "reencode", base.capture.rtsp.reencode),
        fps=fps_str,
        gop=max(1, _get(rtsp_d, "gop", base.capture.rtsp.gop)),
        preset=_get(rtsp_d, "preset", base.capture.rtsp.preset) or "veryfast",
        crf=max(0, min(51, _get(rtsp_d, "crf", base.capture.rtsp.crf))),
        use_wallclock_timestamps=_get(
            rtsp_d, "useWallclockTimestamps", base.capture.rtsp.use_wallclock_timestamps
        ),
    )

    v4l2 = V4l2Config(
        device=_get(v4l2_d, "device", base.capture.v4l2.device) or "/dev/video0",
        framerate=max(1, _get(v4l2_d, "framerate", base.capture.v4l2.framerate)),
        video_size=_get(v4l2_d, "videoSize", base.capture.v4l2.video_size) or "1280x720",
    )

    capture = CaptureParams(
        segment_seconds=max(1, _get(cap_d, "segmentSeconds", base.capture.segment_seconds)),
        pre_segments=max(1, _get(cap_d, "preSegments", base.capture.pre_segments)),
        post_segments=max(1, _get(cap_d, "postSegments", base.capture.post_segments)),
        rtsp=rtsp,
        v4l2=v4l2,
    )

    # cameras
    cameras_raw = data.get("cameras")
    cameras: list[CameraConfig] = base.cameras
    if isinstance(cameras_raw, list) and cameras_raw:
        cameras = []
        for cam in cameras_raw:
            if not isinstance(cam, dict):
                continue
            cam_id = str(cam.get("id") or "").strip()
            if not cam_id:
                continue
            cameras.append(
                CameraConfig(
                    id=cam_id,
                    name=cam.get("name"),
                    enabled=cam.get("enabled", True),
                    source_type=cam.get("sourceType", "rtsp"),
                    rtsp_url=cam.get("rtspUrl"),
                    pico_trigger_token=cam.get("picoTriggerToken"),
                    pre_segments=cam.get("preSegments"),
                    post_segments=cam.get("postSegments"),
                )
            )

    # triggers
    trig_d = data.get("triggers") or {}
    pico_d = trig_d.get("pico") or {}
    gpio_d = trig_d.get("gpio") or {}

    gpio_pin = base.triggers.gpio.pin
    if "pin" in gpio_d and gpio_d["pin"] is not None:
        gpio_pin = int(gpio_d["pin"])

    max_workers = base.triggers.max_workers
    if "maxWorkers" in trig_d and trig_d["maxWorkers"] is not None:
        max_workers = max(1, int(trig_d["maxWorkers"]))

    triggers = TriggerConfig(
        source=(_get(trig_d, "source", base.triggers.source) or "auto").lower(),
        max_workers=max_workers,
        pico=PicoConfig(
            # port permanece None (env); suporte a env: ref se necessário no futuro
            port=None,
            global_token=_get(pico_d, "globalToken", base.triggers.pico.global_token) or "BTN_REPLAY",
        ),
        gpio=GpioConfig(
            pin=gpio_pin,
            debounce_ms=float(_get(gpio_d, "debounceMs", base.triggers.gpio.debounce_ms)),
            cooldown_seconds=float(_get(gpio_d, "cooldownSeconds", base.triggers.gpio.cooldown_seconds)),
        ),
    )

    # processing
    proc_d = data.get("processing") or {}
    wm_d = proc_d.get("watermark") or {}

    processing = ProcessingConfig(
        light_mode=_get(proc_d, "lightMode", base.processing.light_mode),
        max_attempts=max(1, _get(proc_d, "maxAttempts", base.processing.max_attempts)),
        vertical_format=_get(proc_d, "verticalFormat", base.processing.vertical_format),
        hq_crf=max(0, min(51, _get(proc_d, "hqCrf", base.processing.hq_crf))),
        hq_preset=_get(proc_d, "hqPreset", base.processing.hq_preset) or "medium",
        lm_crf=max(0, min(51, _get(proc_d, "lmCrf", base.processing.lm_crf))),
        lm_preset=_get(proc_d, "lmPreset", base.processing.lm_preset) or "veryfast",
        watermark=WatermarkConfig(
            relative_width=max(
                0.01, _get(wm_d, "relativeWidth", base.processing.watermark.relative_width)
            ),
            opacity=max(
                0.0, min(1.0, _get(wm_d, "opacity", base.processing.watermark.opacity))
            ),
            margin=max(0, _get(wm_d, "margin", base.processing.watermark.margin)),
        ),
    )

    # operationWindow
    win_d = data.get("operationWindow") or {}
    operation_window = OperationWindowConfig(
        time_zone=_get(win_d, "timeZone", base.operation_window.time_zone) or "America/Sao_Paulo",
        start=_get(win_d, "start", base.operation_window.start) or "07:00",
        end=_get(win_d, "end", base.operation_window.end) or "23:30",
    )

    # mqtt — credenciais username/password NÃO vêm de config.json
    mqtt_d = data.get("mqtt") or {}
    broker_d = mqtt_d.get("broker") or {}

    mqtt_host = _get(broker_d, "host", base.mqtt.broker.host) or ""
    mqtt_port = max(1, _get(broker_d, "port", base.mqtt.broker.port))
    mqtt_tls = _get(broker_d, "tls", base.mqtt.broker.tls)

    mqtt = MqttParams(
        enabled=_get(mqtt_d, "enabled", base.mqtt.enabled),
        broker=MqttBrokerConfig(host=mqtt_host, port=mqtt_port, tls=mqtt_tls),
        keepalive_seconds=max(5, _get(mqtt_d, "keepaliveSeconds", base.mqtt.keepalive_seconds)),
        heartbeat_interval_seconds=max(
            5, _get(mqtt_d, "heartbeatIntervalSeconds", base.mqtt.heartbeat_interval_seconds)
        ),
        topic_prefix=_get(mqtt_d, "topicPrefix", base.mqtt.topic_prefix) or "grn",
        qos=max(0, min(2, _get(mqtt_d, "qos", base.mqtt.qos))),
        retain_presence=_get(mqtt_d, "retainPresence", base.mqtt.retain_presence),
    )

    return OperationalConfig(
        config_version=_get(data, "version", base.config_version),
        updated_at=data.get("updatedAt"),
        capture=capture,
        cameras=cameras,
        triggers=triggers,
        processing=processing,
        operation_window=operation_window,
        mqtt=mqtt,
    )


# ---------------------------------------------------------------------------
# Carregamento efetivo (defaults → env → config.json)
# ---------------------------------------------------------------------------


def _load_effective_config(config_path: Optional[Path] = None) -> OperationalConfig:
    """Carrega a configuração final aplicando a política de precedência."""
    path = config_path or _default_config_path()

    # Passo 1 + 2: defaults embutidos → leitura de env (fallback legado)
    cfg = _build_from_env()

    # Passo 3: config.json (vence para parâmetros operacionais quando presente)
    if not path.exists():
        _logger.info(
            "config.json não encontrado em %s — usando configuração via env/defaults.", path
        )
        return cfg

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning(
            "Falha ao ler config.json (%s): %s — usando configuração via env/defaults.", path, exc
        )
        return cfg

    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.error(
            "config.json inválido (JSON malformado) em %s: %s — usando configuração via env/defaults.",
            path, exc,
        )
        return cfg

    if not isinstance(data, dict):
        _logger.error(
            "config.json deve ser um objeto JSON — usando configuração via env/defaults."
        )
        return cfg

    errors = validate_config_dict(data)
    if errors:
        _logger.error(
            "config.json rejeitado por erros de validação em %s:\n  %s\n"
            "Usando configuração via env/defaults.",
            path,
            "\n  ".join(errors),
        )
        return cfg

    cfg = _apply_json(cfg, data)
    _logger.info(
        "config.json carregado: %s (version=%s, updatedAt=%s)",
        path, cfg.config_version, cfg.updated_at or "n/a",
    )
    return cfg


# ---------------------------------------------------------------------------
# Singleton com cache (thread-safe)
# ---------------------------------------------------------------------------

_config_cache: Optional[OperationalConfig] = None
_config_lock = threading.Lock()


def get_effective_config(config_path: Optional[Path] = None) -> OperationalConfig:
    """Retorna a configuração operacional efetiva (singleton com cache).

    Na primeira chamada carrega e valida; chamadas subsequentes retornam
    o valor em cache sem I/O adicional.

    Para forçar recarga (ex: testes), use reset_config_cache().
    """
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            _config_cache = _load_effective_config(config_path)
    return _config_cache


def reset_config_cache() -> None:
    """Limpa o cache forçando recarregamento na próxima chamada.

    Útil para testes e para hot-reload futuro de parâmetros seguros.
    """
    global _config_cache
    with _config_lock:
        _config_cache = None
