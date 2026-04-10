"""Validação de campos do config.json.

Valida tipos, ranges, enums e consistência dos campos operacionais.
Não importa nada externo além da stdlib.
"""

from __future__ import annotations

from typing import Any

_VALID_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
}
_VALID_TRIGGER_SOURCES = {"auto", "gpio", "pico", "both"}
_VALID_SOURCE_TYPES = {"rtsp", "v4l2"}


class ConfigValidationError(Exception):
    """Levantado quando config.json contém campos inválidos."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("config.json inválido:\n  " + "\n  ".join(errors))


def _is_valid_hhmm(value: str) -> bool:
    """Retorna True para strings no formato HH:MM com valores válidos."""
    try:
        parts = value.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        return False


def validate_config_dict(data: dict[str, Any]) -> list[str]:
    """Valida um dict carregado de config.json.

    Retorna lista de mensagens de erro (vazia = válido).
    Não levanta exceção — deixa o chamador decidir o que fazer.
    """
    errors: list[str] = []

    # --- version ---
    version = data.get("version")
    if version is not None and (not isinstance(version, int) or version < 1):
        errors.append("version deve ser inteiro >= 1")

    # --- capture ---
    capture = data.get("capture") or {}
    if not isinstance(capture, dict):
        errors.append("capture deve ser um objeto")
        capture = {}

    seg = capture.get("segmentSeconds")
    if seg is not None and (not isinstance(seg, int) or not (1 <= seg <= 10)):
        errors.append("capture.segmentSeconds deve ser inteiro entre 1 e 10")

    pre = capture.get("preSegments")
    if pre is not None and (not isinstance(pre, int) or not (1 <= pre <= 60)):
        errors.append("capture.preSegments deve ser inteiro entre 1 e 60")

    post = capture.get("postSegments")
    if post is not None and (not isinstance(post, int) or not (1 <= post <= 60)):
        errors.append("capture.postSegments deve ser inteiro entre 1 e 60")

    rtsp = capture.get("rtsp") or {}
    if not isinstance(rtsp, dict):
        errors.append("capture.rtsp deve ser um objeto")
        rtsp = {}

    mr = rtsp.get("maxRetries")
    if mr is not None and (not isinstance(mr, int) or not (1 <= mr <= 30)):
        errors.append("capture.rtsp.maxRetries deve ser inteiro entre 1 e 30")

    to_ = rtsp.get("timeoutSeconds")
    if to_ is not None and (not isinstance(to_, int) or not (1 <= to_ <= 60)):
        errors.append("capture.rtsp.timeoutSeconds deve ser inteiro entre 1 e 60")

    sc = rtsp.get("startupCheckSeconds")
    if sc is not None and (not isinstance(sc, (int, float)) or not (0.1 <= sc <= 30)):
        errors.append("capture.rtsp.startupCheckSeconds deve ser float entre 0.1 e 30")

    gop = rtsp.get("gop")
    if gop is not None and (not isinstance(gop, int) or not (1 <= gop <= 300)):
        errors.append("capture.rtsp.gop deve ser inteiro entre 1 e 300")

    preset = rtsp.get("preset")
    if preset is not None and preset not in _VALID_PRESETS:
        errors.append(
            f"capture.rtsp.preset inválido: {preset!r}. Válidos: {sorted(_VALID_PRESETS)}"
        )

    crf = rtsp.get("crf")
    if crf is not None and (not isinstance(crf, int) or not (0 <= crf <= 51)):
        errors.append("capture.rtsp.crf deve ser inteiro entre 0 e 51")

    fps = rtsp.get("fps")
    if fps is not None:
        if isinstance(fps, (int, float)):
            if not (1 <= fps <= 120):
                errors.append("capture.rtsp.fps deve ser número entre 1 e 120")
        elif isinstance(fps, str):
            if fps:
                try:
                    fps_val = float(fps)
                    if not (1 <= fps_val <= 120):
                        errors.append("capture.rtsp.fps (string) deve representar valor entre 1 e 120")
                except ValueError:
                    errors.append("capture.rtsp.fps deve ser número ou string numérica vazia")
        else:
            errors.append("capture.rtsp.fps deve ser número, string numérica ou null")

    v4l2 = capture.get("v4l2") or {}
    if not isinstance(v4l2, dict):
        errors.append("capture.v4l2 deve ser um objeto")
        v4l2 = {}

    fr = v4l2.get("framerate")
    if fr is not None and (not isinstance(fr, int) or not (1 <= fr <= 120)):
        errors.append("capture.v4l2.framerate deve ser inteiro entre 1 e 120")

    vs = v4l2.get("videoSize")
    if vs is not None:
        if not isinstance(vs, str) or not vs:
            errors.append("capture.v4l2.videoSize deve ser string no formato 'WxH'")
        elif "x" not in vs.lower():
            errors.append(f"capture.v4l2.videoSize inválido: {vs!r} (esperado ex: '1280x720')")

    # --- cameras ---
    cameras = data.get("cameras")
    if cameras is not None:
        if not isinstance(cameras, list):
            errors.append("cameras deve ser uma lista")
        else:
            for i, cam in enumerate(cameras):
                if not isinstance(cam, dict):
                    errors.append(f"cameras[{i}] deve ser um objeto")
                    continue
                if not cam.get("id"):
                    errors.append(f"cameras[{i}].id é obrigatório e não pode ser vazio")
                src_type = cam.get("sourceType", "rtsp")
                if src_type not in _VALID_SOURCE_TYPES:
                    errors.append(
                        f"cameras[{i}].sourceType inválido: {src_type!r}. "
                        f"Válidos: {sorted(_VALID_SOURCE_TYPES)}"
                    )
                cam_pre = cam.get("preSegments")
                if cam_pre is not None and (not isinstance(cam_pre, int) or not (1 <= cam_pre <= 60)):
                    errors.append(f"cameras[{i}].preSegments deve ser inteiro entre 1 e 60")
                cam_post = cam.get("postSegments")
                if cam_post is not None and (not isinstance(cam_post, int) or not (1 <= cam_post <= 60)):
                    errors.append(f"cameras[{i}].postSegments deve ser inteiro entre 1 e 60")

    # --- triggers ---
    triggers = data.get("triggers") or {}
    if not isinstance(triggers, dict):
        errors.append("triggers deve ser um objeto")
        triggers = {}

    source = triggers.get("source")
    if source is not None and source not in _VALID_TRIGGER_SOURCES:
        errors.append(
            f"triggers.source inválido: {source!r}. Válidos: {sorted(_VALID_TRIGGER_SOURCES)}"
        )

    mw = triggers.get("maxWorkers")
    if mw is not None and (not isinstance(mw, int) or not (1 <= mw <= 32)):
        errors.append("triggers.maxWorkers deve ser null ou inteiro entre 1 e 32")

    gpio = triggers.get("gpio") or {}
    if not isinstance(gpio, dict):
        errors.append("triggers.gpio deve ser um objeto")
        gpio = {}

    pin = gpio.get("pin")
    if pin is not None and (not isinstance(pin, int) or not (0 <= pin <= 40)):
        errors.append("triggers.gpio.pin deve ser inteiro entre 0 e 40 (BCM)")

    deb = gpio.get("debounceMs")
    if deb is not None and (not isinstance(deb, (int, float)) or not (0 <= deb <= 5000)):
        errors.append("triggers.gpio.debounceMs deve ser float entre 0 e 5000")

    cd = gpio.get("cooldownSeconds")
    if cd is not None and (not isinstance(cd, (int, float)) or not (0 <= cd <= 3600)):
        errors.append("triggers.gpio.cooldownSeconds deve ser float entre 0 e 3600")

    # --- processing ---
    processing = data.get("processing") or {}
    if not isinstance(processing, dict):
        errors.append("processing deve ser um objeto")
        processing = {}

    ma = processing.get("maxAttempts")
    if ma is not None and (not isinstance(ma, int) or not (1 <= ma <= 20)):
        errors.append("processing.maxAttempts deve ser inteiro entre 1 e 20")

    hq_crf = processing.get("hqCrf")
    if hq_crf is not None and (not isinstance(hq_crf, int) or not (0 <= hq_crf <= 51)):
        errors.append("processing.hqCrf deve ser inteiro entre 0 e 51")

    hq_preset = processing.get("hqPreset")
    if hq_preset is not None and hq_preset not in _VALID_PRESETS:
        errors.append(
            f"processing.hqPreset inválido: {hq_preset!r}. Válidos: {sorted(_VALID_PRESETS)}"
        )

    lm_crf = processing.get("lmCrf")
    if lm_crf is not None and (not isinstance(lm_crf, int) or not (0 <= lm_crf <= 51)):
        errors.append("processing.lmCrf deve ser inteiro entre 0 e 51")

    lm_preset = processing.get("lmPreset")
    if lm_preset is not None and lm_preset not in _VALID_PRESETS:
        errors.append(
            f"processing.lmPreset inválido: {lm_preset!r}. Válidos: {sorted(_VALID_PRESETS)}"
        )

    wm = processing.get("watermark") or {}
    if not isinstance(wm, dict):
        errors.append("processing.watermark deve ser um objeto")
        wm = {}

    rw = wm.get("relativeWidth")
    if rw is not None and (not isinstance(rw, (int, float)) or not (0 < rw <= 1)):
        errors.append("processing.watermark.relativeWidth deve ser float entre 0 (excl) e 1")

    op = wm.get("opacity")
    if op is not None and (not isinstance(op, (int, float)) or not (0.0 <= op <= 1.0)):
        errors.append("processing.watermark.opacity deve ser float entre 0 e 1")

    margin = wm.get("margin")
    if margin is not None and (not isinstance(margin, int) or not (0 <= margin <= 500)):
        errors.append("processing.watermark.margin deve ser inteiro entre 0 e 500")

    # --- operationWindow ---
    win = data.get("operationWindow") or {}
    if not isinstance(win, dict):
        errors.append("operationWindow deve ser um objeto")
        win = {}

    tz = win.get("timeZone")
    if tz is not None and not isinstance(tz, str):
        errors.append("operationWindow.timeZone deve ser string (ex: 'America/Sao_Paulo')")

    start_t = win.get("start")
    if start_t is not None and (not isinstance(start_t, str) or not _is_valid_hhmm(start_t)):
        errors.append(f"operationWindow.start inválido: {start_t!r} (esperado HH:MM)")

    end_t = win.get("end")
    if end_t is not None and (not isinstance(end_t, str) or not _is_valid_hhmm(end_t)):
        errors.append(f"operationWindow.end inválido: {end_t!r} (esperado HH:MM)")

    # --- mqtt ---
    mqtt = data.get("mqtt") or {}
    if not isinstance(mqtt, dict):
        errors.append("mqtt deve ser um objeto")
        mqtt = {}

    broker = mqtt.get("broker") or {}
    if not isinstance(broker, dict):
        errors.append("mqtt.broker deve ser um objeto")
        broker = {}

    b_port = broker.get("port")
    if b_port is not None and (not isinstance(b_port, int) or not (1 <= b_port <= 65535)):
        errors.append("mqtt.broker.port deve ser inteiro entre 1 e 65535")

    qos = mqtt.get("qos")
    if qos is not None and (not isinstance(qos, int) or qos not in {0, 1, 2}):
        errors.append("mqtt.qos deve ser 0, 1 ou 2")

    ka = mqtt.get("keepaliveSeconds")
    if ka is not None and (not isinstance(ka, int) or not (5 <= ka <= 3600)):
        errors.append("mqtt.keepaliveSeconds deve ser inteiro entre 5 e 3600")

    hi = mqtt.get("heartbeatIntervalSeconds")
    if hi is not None and (not isinstance(hi, int) or not (5 <= hi <= 3600)):
        errors.append("mqtt.heartbeatIntervalSeconds deve ser inteiro entre 5 e 3600")

    return errors
