from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_RASPBERRY_MODEL_FILES = (
    "/proc/device-tree/model",
    "/sys/firmware/devicetree/base/model",
)


def _log_info(logger: Any | None, message: str) -> None:
    if logger is not None:
        logger.info(message)


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def detect_raspberry_model() -> str | None:
    """Retorna o model do hardware quando disponível no Linux."""
    for model_file in _RASPBERRY_MODEL_FILES:
        try:
            raw_model = Path(model_file).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        model = raw_model.replace("\x00", "").strip()
        if model:
            return model
    return None


def is_raspberry_pi(logger: Any | None = None) -> bool:
    """
    Detecta Raspberry Pi automaticamente.

    Override opcional:
    - GN_FORCE_RASPBERRY_PI=1 -> força True
    - GN_FORCE_RASPBERRY_PI=0 -> força False
    """
    force = os.getenv("GN_FORCE_RASPBERRY_PI")
    if force is not None:
        forced = _parse_bool(force, default=False)
        _log_info(logger, f"GN_FORCE_RASPBERRY_PI aplicado: {forced}")
        return forced

    model = detect_raspberry_model()
    if not model:
        return False

    detected = "raspberry pi" in model.lower()
    if detected:
        _log_info(logger, f"Hardware detectado: {model}")
    return detected
