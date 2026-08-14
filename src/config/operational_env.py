"""Persistência da configuração operacional no .env gerenciado do host."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _bool(value: Any) -> str:
    return "1" if bool(value) else "0"


def _nullable(value: Any, formatter=str) -> str:
    return "" if value is None else formatter(value)


def operational_config_to_env(config: dict[str, Any]) -> dict[str, str]:
    capture = config["capture"]
    rtsp = capture["rtsp"]
    v4l2 = capture["v4l2"]
    triggers = config["triggers"]
    gpio = triggers["gpio"]
    processing = config["processing"]
    watermark = processing["watermark"]
    window = config["operationWindow"]
    mqtt = config["mqtt"]
    broker = mqtt["broker"]

    cameras = [
        {
            "id": camera["id"],
            **({"name": camera["name"]} if camera.get("name") is not None else {}),
            "enabled": camera.get("enabled", True),
            "source_type": camera.get("sourceType", "rtsp"),
            **({"rtsp_url": camera["rtspUrl"]} if camera.get("rtspUrl") else {}),
            **(
                {"pico_trigger_token": camera["picoTriggerToken"]}
                if camera.get("picoTriggerToken")
                else {}
            ),
            **(
                {"pre_segments": camera["preSegments"]}
                if camera.get("preSegments") is not None
                else {}
            ),
            **(
                {"post_segments": camera["postSegments"]}
                if camera.get("postSegments") is not None
                else {}
            ),
        }
        for camera in config["cameras"]
    ]

    return {
        "GN_CONFIG_VERSION": str(config.get("version", 1)),
        "GN_CONFIG_UPDATED_AT": str(config.get("updatedAt") or ""),
        "GN_SEG_TIME": str(capture["segmentSeconds"]),
        "GN_RTSP_PRE_SEGMENTS": str(capture["preSegments"]),
        "GN_RTSP_POST_SEGMENTS": str(capture["postSegments"]),
        "GN_RTSP_MAX_RETRIES": str(rtsp["maxRetries"]),
        "GN_RTSP_TIMEOUT": str(rtsp["timeoutSeconds"]),
        "GN_FFMPEG_STARTUP_CHECK_SEC": str(rtsp["startupCheckSeconds"]),
        "GN_RTSP_PROFILE": _nullable(rtsp.get("profile")),
        "GN_RTSP_REENCODE": _nullable(rtsp.get("reencode"), _bool),
        "GN_RTSP_FPS": str(rtsp.get("fps", "")),
        "GN_RTSP_GOP": str(rtsp["gop"]),
        "GN_RTSP_PRESET": str(rtsp["preset"]),
        "GN_RTSP_CRF": str(rtsp["crf"]),
        "GN_RTSP_USE_WALLCLOCK": _bool(rtsp["useWallclockTimestamps"]),
        "GN_RTSP_LOW_LATENCY_INPUT": _bool(rtsp.get("lowLatencyInput", False)),
        "GN_RTSP_LOW_DELAY_CODEC_FLAGS": _bool(rtsp.get("lowDelayCodecFlags", False)),
        "GN_V4L2_DEVICE": str(v4l2["device"]),
        "GN_INPUT_FRAMERATE": str(v4l2["framerate"]),
        "GN_VIDEO_SIZE": str(v4l2["videoSize"]),
        "GN_CAMERAS_JSON": json.dumps(cameras, ensure_ascii=False, separators=(",", ":")),
        "GN_TRIGGER_SOURCE": str(triggers["source"]),
        "GN_TRIGGER_MAX_WORKERS": _nullable(triggers.get("maxWorkers")),
        "GN_PICO_TRIGGER_TOKEN": str(triggers["pico"]["globalToken"]),
        "GN_GPIO_PIN": _nullable(gpio.get("pin")),
        "GN_GPIO_DEBOUNCE_MS": str(gpio["debounceMs"]),
        "GN_GPIO_COOLDOWN_SEC": str(gpio["cooldownSeconds"]),
        "GN_LIGHT_MODE": _bool(processing["lightMode"]),
        "GN_MAX_ATTEMPTS": str(processing["maxAttempts"]),
        "VERTICAL_FORMAT": _bool(processing["verticalFormat"]),
        "GN_HQ_CRF": str(processing["hqCrf"]),
        "GN_HQ_PRESET": str(processing["hqPreset"]),
        "GN_LM_CRF": str(processing["lmCrf"]),
        "GN_LM_PRESET": str(processing["lmPreset"]),
        "GN_WM_REL_WIDTH": str(watermark["relativeWidth"]),
        "GN_WM_OPACITY": str(watermark["opacity"]),
        "GN_WM_MARGIN": str(watermark["margin"]),
        "GN_TIME_ZONE": str(window["timeZone"]),
        "GN_START_TIME": str(window["start"]),
        "GN_END_TIME": str(window["end"]),
        "GN_MQTT_ENABLED": _bool(mqtt["enabled"]),
        "GN_MQTT_HOST": str(broker["host"]),
        "GN_MQTT_PORT": str(broker["port"]),
        "GN_MQTT_TLS": _bool(broker["tls"]),
        "GN_MQTT_KEEPALIVE": str(mqtt["keepaliveSeconds"]),
        "GN_MQTT_HEARTBEAT_INTERVAL_SEC": str(mqtt["heartbeatIntervalSeconds"]),
        "GN_MQTT_TOPIC_PREFIX": str(mqtt["topicPrefix"]),
        "GN_MQTT_QOS": str(mqtt["qos"]),
        "GN_MQTT_RETAIN_PRESENCE": _bool(mqtt["retainPresence"]),
    }


def merge_env_content(content: str, updates: dict[str, str]) -> str:
    remaining = dict(updates)
    output: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        candidate = stripped.removeprefix("export ").strip()
        key = candidate.partition("=")[0].strip() if "=" in candidate else ""
        if key in updates:
            if key in seen:
                continue
            output.append(f"{key}={updates[key]}")
            seen.add(key)
            remaining.pop(key, None)
        else:
            output.append(line)
    if output and output[-1] != "" and remaining:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output).rstrip("\n") + "\n"


def persist_operational_config(env_path: Path, config: dict[str, Any]) -> str:
    if not env_path.is_file():
        raise OSError(f".env gerenciado não encontrado: {env_path}")
    if not os.access(env_path, os.R_OK | os.W_OK):
        raise OSError(f".env gerenciado sem permissão de leitura/escrita: {env_path}")

    original = env_path.read_text(encoding="utf-8")
    updates = operational_config_to_env(config)
    current = _parse_env_map(original)
    broker_url = current.get("GN_MQTT_BROKER_URL", "")
    if broker_url:
        parsed = urlsplit(broker_url)
        broker = config["mqtt"]["broker"]
        userinfo = ""
        if parsed.username is not None:
            userinfo = parsed.username
            if parsed.password is not None:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        updates["GN_MQTT_BROKER_URL"] = urlunsplit(
            (
                "mqtts" if broker["tls"] else "mqtt",
                f"{userinfo}{broker['host']}:{broker['port']}",
                "",
                "",
                "",
            )
        )
    merged = merge_env_content(original, updates)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup_path = env_path.with_suffix(f".bak.grn.config.{timestamp}")
    shutil.copy2(env_path, backup_path)
    os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)

    fd, temp_path = tempfile.mkstemp(dir=env_path.parent, prefix=".env.tmp.", suffix=".grn")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(merged)
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, env_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return original


def _parse_env_map(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.removeprefix("export ").partition("=")[::2]
        value = value.strip()
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def restore_env_content(env_path: Path, content: str) -> None:
    fd, temp_path = tempfile.mkstemp(dir=env_path.parent, prefix=".env.rollback.", suffix=".grn")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, env_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
