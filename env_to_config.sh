#!/usr/bin/env bash
# =============================================================================
# env_to_config.sh
# Converte .env do grava_nois_system para config.json (configuração gerenciada)
#
# Uso:
#   ./env_to_config.sh [ENV_FILE] [OUTPUT_FILE] [--dry-run]
#
# Exemplos:
#   ./env_to_config.sh                        # .env → config.json
#   ./env_to_config.sh .env.prod              # .env.prod → config.json
#   ./env_to_config.sh .env config.json       # explícito
#   ./env_to_config.sh .env /tmp/cfg.json     # saída alternativa
#   ./env_to_config.sh .env config.json --dry-run  # só exibe, não grava
#   sudo ./env_to_config.sh /opt/.grn/config/.env /opt/.grn/config/config.json
#
# O que este script faz:
#   - Lê variáveis operacionais do .env e gera config.json equivalente
#   - Detecta credenciais RTSP e substitui por referências 'env:VAR_NAME'
#   - NÃO migra segredos, tokens, identidade de device ou flags de dev
#   - Faz backup automático se config.json já existir
#
# Requisitos:
#   - Python 3.10+ (já exigido pelo sistema)
#
# Política de segurança:
#   Variáveis que NUNCA vão para config.json (permanecem em env):
#     DEVICE_SECRET, GN_DEVICE_SECRET
#     GN_API_TOKEN, API_TOKEN
#     GN_MQTT_PASSWORD, GN_MQTT_USERNAME
#     DEVICE_ID, GN_DEVICE_ID
#     GN_CLIENT_ID, CLIENT_ID
#     GN_VENUE_ID, VENUE_ID
#     GN_API_BASE, API_BASE_URL
#     GN_BUFFER_DIR, GN_LOG_DIR
#     GN_PICO_PORT
#     DEV, DEV_VIDEO_MODE, GN_HMAC_DRY_RUN, GN_FORCE_RASPBERRY_PI
#     GN_AGENT_VERSION
#     GN_RUN_CAMERA_INTEGRATION, GN_CAMERA_INTEGRATION_OUTPUT_DIR
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Argumentos
# ---------------------------------------------------------------------------
DEFAULT_ENV_FILE=".env"
LEGACY_ENV_FILE="/opt/.grn/config/.env"
ENV_FILE="$DEFAULT_ENV_FILE"
OUTPUT_FILE="config.json"
DRY_RUN="false"

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN="true" ;;
        --help|-h)
            sed -n '2,/^# ===/{ /^# ===/d; s/^# \{0,1\}//; p }' "$0"
            exit 0
            ;;
        -*)
            echo "Opção desconhecida: $arg. Use --help para ajuda." >&2
            exit 1
            ;;
        *)
            if [[ "$ENV_FILE" == ".env" && "$arg" != "$OUTPUT_FILE" ]]; then
                ENV_FILE="$arg"
            elif [[ "$OUTPUT_FILE" == "config.json" ]]; then
                OUTPUT_FILE="$arg"
            fi
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Verificações
# ---------------------------------------------------------------------------
if ! command -v python3 &>/dev/null; then
    echo "ERRO: python3 não encontrado. Python 3.10+ é necessário pelo sistema." >&2
    exit 1
fi

if [[ "$ENV_FILE" == "$DEFAULT_ENV_FILE" && ! -f "$ENV_FILE" && -f "$LEGACY_ENV_FILE" ]]; then
    ENV_FILE="$LEGACY_ENV_FILE"
    OUTPUT_FILE="/opt/.grn/config/config.json"
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERRO: arquivo de ambiente não encontrado: $ENV_FILE" >&2
    echo "Dica: copie .env.example para .env e configure antes de executar." >&2
    echo "Em devices legados, tente: sudo $0 /opt/.grn/config/.env /opt/.grn/config/config.json" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Lógica principal em Python (embutida no bash via heredoc)
# Python é usado para garantir:
#   - Parse robusto do .env (valores com =, aspas, espaços)
#   - Geração de JSON correto (escape, tipos, null)
#   - Parse de GN_CAMERAS_JSON e URLs RTSP
# ---------------------------------------------------------------------------
python3 - "$ENV_FILE" "$OUTPUT_FILE" "$DRY_RUN" <<'PYEOF'
import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

env_file   = sys.argv[1]
output_file = sys.argv[2]
dry_run    = sys.argv[3].lower() == "true"

# ---------------------------------------------------------------------------
# Parse seguro do .env (sem eval/source)
# ---------------------------------------------------------------------------
def parse_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n\r")
            stripped = line.strip()
            # ignora comentários e linhas vazias
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            key = key.strip()
            # ignora chaves inválidas (com espaços ou export)
            key = key.removeprefix("export").strip()
            if not key or " " in key:
                continue
            val = val.strip()
            # remove aspas envolventes (simples ou duplas)
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            # ignora valores vazios — mantém o default do loader
            env[key] = val
    return env


env = parse_env_file(env_file)

# ---------------------------------------------------------------------------
# Helpers de extração de valores
# ---------------------------------------------------------------------------
def _str(key: str, default: str = "", *aliases: str) -> str:
    for k in (key,) + aliases:
        v = env.get(k, "")
        if v:
            return v
    return default


def _int(key: str, default: int, *aliases: str) -> int:
    val = _str(key, "", *aliases)
    if not val:
        return default
    try:
        return max(1, int(float(val)))
    except (ValueError, TypeError):
        return default


def _int_range(
    key: str,
    default: int,
    min_value: int,
    max_value: int | None = None,
    *aliases: str,
) -> int:
    val = _str(key, "", *aliases)
    if not val:
        parsed = default
    else:
        try:
            parsed = int(float(val))
        except (ValueError, TypeError):
            parsed = default
    parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _int_or_none(key: str, *aliases: str):
    val = _str(key, "", *aliases)
    if not val:
        return None
    try:
        return max(1, int(float(val)))
    except (ValueError, TypeError):
        return None


def _float(key: str, default: float, *aliases: str) -> float:
    val = _str(key, "", *aliases)
    if not val:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _bool(key: str, default: bool, *aliases: str) -> bool:
    val = _str(key, "", *aliases)
    if not val:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def _bool_value(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


# ---------------------------------------------------------------------------
# Detecção de credenciais em URL RTSP
# ---------------------------------------------------------------------------
_RTSP_CRED_RE = re.compile(r"^rtsp://[^@/]+:[^@/]+@", re.IGNORECASE)

def has_rtsp_credentials(url: str) -> bool:
    """Retorna True se a URL RTSP contém 'user:pass@'."""
    return bool(_RTSP_CRED_RE.match(url or ""))


def cam_id_to_env_var(cam_id: str) -> str:
    """Transforma id da câmera em nome de variável de ambiente.

    Exemplos:
      'cam01'       → 'GN_CAM01_RTSP_URL'
      'cam_quadra1' → 'GN_CAM_QUADRA1_RTSP_URL'
    """
    clean = re.sub(r"[^a-zA-Z0-9]", "_", cam_id).upper().strip("_")
    return f"GN_{clean}_RTSP_URL"


# ---------------------------------------------------------------------------
# Acumuladores de mensagens
# ---------------------------------------------------------------------------
warnings: list[str] = []
credential_notes: list[str] = []
skipped_secrets: list[str] = []

# ---------------------------------------------------------------------------
# Seção cameras
# ---------------------------------------------------------------------------
def build_cameras_section() -> list[dict]:
    cameras_json_raw = _str("GN_CAMERAS_JSON", "")
    rtsp_urls_csv    = _str("GN_RTSP_URLS", "")
    rtsp_url_single  = _str("GN_RTSP_URL", "")

    pre_seg  = _int("GN_RTSP_PRE_SEGMENTS", 6)
    post_seg = _int("GN_RTSP_POST_SEGMENTS", 3)

    cameras: list[dict] = []

    # --- Fonte 1: GN_CAMERAS_JSON ---
    if cameras_json_raw:
        try:
            raw_list = json.loads(cameras_json_raw)
        except json.JSONDecodeError as exc:
            warnings.append(
                f"GN_CAMERAS_JSON inválido (JSON malformado): {exc} "
                "— câmeras ignoradas; seção cameras ficará vazia."
            )
            return []

        if not isinstance(raw_list, list):
            warnings.append("GN_CAMERAS_JSON deve ser lista JSON — câmeras ignoradas.")
            return []

        for idx, cam in enumerate(raw_list):
            if not isinstance(cam, dict):
                continue
            cam_id   = str(cam.get("id") or f"cam{idx + 1:02d}").strip()
            rtsp_url = str(cam.get("rtsp_url") or "").strip()
            name     = cam.get("name")
            enabled  = _bool_value(cam.get("enabled"), True)
            pico_tok = cam.get("pico_trigger_token") or None
            src_type = cam.get("sourceType") or cam.get("source_type") or "rtsp"

            rtsp_ref: str | None = None
            if rtsp_url:
                if has_rtsp_credentials(rtsp_url):
                    env_var  = cam_id_to_env_var(cam_id)
                    rtsp_ref = f"env:{env_var}"
                    credential_notes.append(
                        f"  câmera '{cam_id}': credencial RTSP detectada → "
                        f"rtspUrl='{rtsp_ref}'\n"
                        f"    Adicione ao .env: {env_var}={rtsp_url}"
                    )
                else:
                    rtsp_ref = rtsp_url

            entry: dict = {
                "id":      cam_id,
                "enabled": enabled,
                "sourceType": src_type,
            }
            if name is not None:
                entry["name"] = name
            if rtsp_ref:
                entry["rtspUrl"] = rtsp_ref
            if pico_tok:
                entry["picoTriggerToken"] = pico_tok
            entry["preSegments"]  = int(cam.get("pre_segments",  pre_seg))
            entry["postSegments"] = int(cam.get("post_segments", post_seg))
            cameras.append(entry)

        return cameras

    # --- Fonte 2: GN_RTSP_URLS (CSV) ---
    if rtsp_urls_csv:
        urls = [u.strip() for u in rtsp_urls_csv.split(",") if u.strip()]
        for idx, url in enumerate(urls, start=1):
            cam_id = f"cam{idx:02d}"
            if has_rtsp_credentials(url):
                env_var  = f"GN_CAM{idx:02d}_RTSP_URL"
                rtsp_ref = f"env:{env_var}"
                credential_notes.append(
                    f"  câmera '{cam_id}': credencial RTSP detectada → "
                    f"rtspUrl='{rtsp_ref}'\n"
                    f"    Adicione ao .env: {env_var}={url}"
                )
            else:
                rtsp_ref = url
            cameras.append({
                "id":           cam_id,
                "enabled":      True,
                "sourceType":   "rtsp",
                "rtspUrl":      rtsp_ref,
                "preSegments":  pre_seg,
                "postSegments": post_seg,
            })
        return cameras

    # --- Fonte 3: GN_RTSP_URL (câmera única) ---
    if rtsp_url_single:
        if has_rtsp_credentials(rtsp_url_single):
            rtsp_ref = "env:GN_RTSP_URL"
            credential_notes.append(
                f"  câmera 'cam01': credencial RTSP detectada → rtspUrl='{rtsp_ref}'\n"
                f"    GN_RTSP_URL já está no .env — o loader resolverá automaticamente."
            )
        else:
            rtsp_ref = rtsp_url_single
        return [{
            "id":           "cam01",
            "enabled":      True,
            "sourceType":   "rtsp",
            "rtspUrl":      rtsp_ref,
            "preSegments":  _int("GN_RTSP_PRE_SEGMENTS", 6),
            "postSegments": _int("GN_RTSP_POST_SEGMENTS", 3),
        }]

    # --- Sem fonte RTSP: câmera vazia (V4L2 via fallback do loader) ---
    warnings.append(
        "Nenhuma fonte RTSP encontrada (GN_CAMERAS_JSON / GN_RTSP_URLS / GN_RTSP_URL). "
        "O sistema usará V4L2 local como fallback."
    )
    return []


# ---------------------------------------------------------------------------
# Parse de broker MQTT (URL ou host:port)
# ---------------------------------------------------------------------------
def parse_mqtt_broker() -> tuple[str, int, bool]:
    broker_url  = _str("GN_MQTT_BROKER_URL", "") or _str("GN_MQTT_HOST", "")
    default_port = _int("GN_MQTT_PORT", 1883)

    if not broker_url:
        return "", default_port, False

    if "://" not in broker_url:
        if ":" in broker_url and broker_url.count(":") == 1:
            host, raw_port = broker_url.split(":", 1)
            try:
                return host.strip(), max(1, int(raw_port)), False
            except ValueError:
                return host.strip(), default_port, False
        return broker_url, default_port, False

    parsed   = urlparse(broker_url)
    scheme   = (parsed.scheme or "").lower()
    host     = parsed.hostname or ""
    port     = parsed.port or default_port
    use_tls  = scheme in {"mqtts", "ssl", "tls"}
    if parsed.username or parsed.password:
        credential_notes.append(
            "  MQTT: credenciais detectadas em GN_MQTT_BROKER_URL — "
            "apenas host/porta/TLS foram migrados; mantenha usuário/senha em env."
        )
    return host, port, use_tls


mqtt_host, mqtt_port_base, mqtt_tls_from_url = parse_mqtt_broker()
mqtt_port = _int("GN_MQTT_PORT", mqtt_port_base)
mqtt_tls  = _bool("GN_MQTT_TLS", mqtt_tls_from_url)

# ---------------------------------------------------------------------------
# Variáveis que NÃO são migradas (segredos / identidade / deploy)
# ---------------------------------------------------------------------------
_SECRETS = [
    "DEVICE_SECRET", "GN_DEVICE_SECRET",
    "GN_API_TOKEN",  "API_TOKEN",
    "GN_MQTT_PASSWORD", "GN_MQTT_USERNAME",
    "DEVICE_ID",     "GN_DEVICE_ID",
    "GN_CLIENT_ID",  "CLIENT_ID",
    "GN_VENUE_ID",   "VENUE_ID",
    "GN_API_BASE",   "API_BASE_URL",
    "GN_BUFFER_DIR", "GN_LOG_DIR",
    "GN_PICO_PORT",
    "DEV", "DEV_VIDEO_MODE",
    "GN_HMAC_DRY_RUN", "HMAC_DRY_RUN",
    "GN_FORCE_RASPBERRY_PI",
    "GN_AGENT_VERSION",
    "GN_RUN_CAMERA_INTEGRATION", "GN_CAMERA_INTEGRATION_OUTPUT_DIR",
    "GRAVA_NOIS_SYSTEM_IMAGE",
]

for sv in _SECRETS:
    if env.get(sv, ""):
        skipped_secrets.append(sv)

# ---------------------------------------------------------------------------
# GPIO pin
# ---------------------------------------------------------------------------
gpio_pin_raw = env.get("GN_GPIO_PIN", "") or env.get("GPIO_PIN", "")
gpio_pin: int | None = None
if gpio_pin_raw:
    try:
        gpio_pin = int(gpio_pin_raw.strip())
    except ValueError:
        warnings.append(f"GN_GPIO_PIN inválido ({gpio_pin_raw!r}) — campo omitido (null)")

# max_workers (None = usa número de câmeras ativas como padrão)
max_workers = _int_or_none("GN_TRIGGER_MAX_WORKERS")

# fps pode ser string vazia (sem filtro)
fps_raw = env.get("GN_RTSP_FPS", "").strip()

# ---------------------------------------------------------------------------
# Monta config dict
# ---------------------------------------------------------------------------
config: dict = {
    "version":   1,
    "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "capture": {
        "segmentSeconds": _int("GN_SEG_TIME", 1),
        "preSegments":    _int("GN_RTSP_PRE_SEGMENTS", 6),
        "postSegments":   _int("GN_RTSP_POST_SEGMENTS", 3),
        "rtsp": {
            "maxRetries":            _int("GN_RTSP_MAX_RETRIES", 10),
            "timeoutSeconds":        _int("GN_RTSP_TIMEOUT", 5),
            "startupCheckSeconds":   max(0.1, _float("GN_FFMPEG_STARTUP_CHECK_SEC", 1.0)),
            "reencode":              _bool("GN_RTSP_REENCODE", True),
            "fps":                   fps_raw,
            "gop":                   max(1, _int("GN_RTSP_GOP", 25)),
            "preset":                _str("GN_RTSP_PRESET", "veryfast") or "veryfast",
            "crf":                   _int_range("GN_RTSP_CRF", 23, 0, 51),
            "useWallclockTimestamps":_bool("GN_RTSP_USE_WALLCLOCK", False),
        },
        "v4l2": {
            "device":    "/dev/video0",
            "framerate": _int("GN_INPUT_FRAMERATE", 30),
            "videoSize": _str("GN_VIDEO_SIZE", "1280x720") or "1280x720",
        },
    },
    "cameras": build_cameras_section(),
    "triggers": {
        "source":     (_str("GN_TRIGGER_SOURCE", "auto") or "auto").lower(),
        "maxWorkers": max_workers,
        "pico": {
            "globalToken": _str("GN_PICO_TRIGGER_TOKEN", "BTN_REPLAY") or "BTN_REPLAY",
        },
        "gpio": {
            "pin":             gpio_pin,
            "debounceMs":      _float("GN_GPIO_DEBOUNCE_MS", 300.0),
            "cooldownSeconds": _float("GN_GPIO_COOLDOWN_SEC", 120.0),
        },
    },
    "processing": {
        "lightMode":      _bool("GN_LIGHT_MODE", False),
        "maxAttempts":    max(1, _int("GN_MAX_ATTEMPTS", 3)),
        "mobileFormat":   _bool("MOBILE_FORMAT", True),
        "verticalFormat": _bool("VERTICAL_FORMAT", True),
        "watermark": {
            "preset":        _str("GN_WM_PRESET", "veryfast") or "veryfast",
            "relativeWidth": max(0.01, _float("GN_WM_REL_WIDTH", 0.18)),
            "opacity":       max(0.0, min(1.0, _float("GN_WM_OPACITY", 0.8))),
            "margin":        _int_range("GN_WM_MARGIN", 24, 0, 500),
        },
    },
    "operationWindow": {
        "timeZone": _str("GN_TIME_ZONE", "America/Sao_Paulo") or "America/Sao_Paulo",
        "start":    _str("GN_START_TIME", "07:00") or "07:00",
        "end":      _str("GN_END_TIME",   "23:30") or "23:30",
    },
    "mqtt": {
        "enabled": _bool("GN_MQTT_ENABLED", False),
        "broker": {
            "host": mqtt_host,
            "port": mqtt_port,
            "tls":  mqtt_tls,
        },
        "keepaliveSeconds":           _int("GN_MQTT_KEEPALIVE", 60),
        "heartbeatIntervalSeconds":   _int("GN_MQTT_HEARTBEAT_INTERVAL_SEC", 30),
        "topicPrefix":                _str("GN_MQTT_TOPIC_PREFIX", "grn") or "grn",
        "qos":                        _int_range("GN_MQTT_QOS", 1, 0, 2),
        "retainPresence":             _bool("GN_MQTT_RETAIN_PRESENCE", True),
    },
}

json_output = json.dumps(config, indent=2, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Relatório de saída
# ---------------------------------------------------------------------------
SEP = "─" * 60

print(SEP)
print("  env_to_config.sh — Conversão .env → config.json")
print(SEP)
print(f"  Fonte : {Path(env_file).resolve()}")
print(f"  Saída : {Path(output_file).resolve()}")
if dry_run:
    print("  Modo  : DRY RUN (nenhum arquivo será gravado)")
print()

if skipped_secrets:
    print("⚠  NÃO migradas (permanecem em env — segredos/identidade):")
    for s in skipped_secrets:
        print(f"   • {s}")
    print()

if credential_notes:
    print("🔑 Credenciais detectadas — valores sensíveis não foram gravados no config.json:")
    for note in credential_notes:
        print(note)
    print()

if warnings:
    print("⚠  Avisos:")
    for w in warnings:
        print(f"   • {w}")
    print()

if dry_run:
    print("── DRY RUN: config.json abaixo (não foi gravado) ──")
    print()
    print(json_output)
    print()
    print(SEP)
    sys.exit(0)

# ---------------------------------------------------------------------------
# Grava o arquivo
# ---------------------------------------------------------------------------
output_path = Path(output_file)

if output_path.exists():
    backup_path = output_path.with_suffix(".json.bak")
    output_path.rename(backup_path)
    print(f"✓  Backup criado: {backup_path}")

output_path.write_text(json_output + "\n", encoding="utf-8")
print(f"✓  config.json gerado: {output_path.resolve()}")
print()

# Próximos passos
has_env_refs = any(
    str(cam.get("rtspUrl", "")).startswith("env:")
    for cam in config.get("cameras", [])
)

print("Próximos passos:")
step_no = 1
print(f"  {step_no}. Revise o config.json gerado antes de usar em produção.")
step_no += 1
if skipped_secrets:
    print(f"  {step_no}. Mantenha os segredos/identidade listados acima no .env.")
    step_no += 1
if has_env_refs:
    print(f"  {step_no}. Para câmeras com 'env:VAR', certifique-se que as variáveis")
    print("     estão definidas no .env (ex: GN_CAM01_RTSP_URL=rtsp://user:pass@...).")
    step_no += 1
if str(output_path) != "config.json":
    print(f"  {step_no}. Se o arquivo não estiver na raiz runtime do container, monte-o no")
    print("     compose e defina GN_CONFIG_PATH para o path visto dentro do container.")
    print("     Ex: GN_CONFIG_PATH=/usr/src/app/config.json")
    step_no += 1
    print(f"  {step_no}. Reinicie o serviço: docker compose restart")
else:
    print(f"  {step_no}. Reinicie o serviço: docker compose restart")
print()
print(f"  Documentação: docs/specs/system/CONFIGURATION.md")
print(SEP)
PYEOF
