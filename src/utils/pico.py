from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any

from src.config.config_loader import get_effective_config
from src.utils.device import is_raspberry_pi

_PICO_HINTS = (
    "pico",
    "rp2040",
    "raspberry",
    "board in fs mode",
)
_VALID_TRIGGER_SOURCES = {"auto", "gpio", "pico", "both"}


def _is_device_path(path: str) -> bool:
    """Retorna True para caminhos em /dev considerados válidos."""
    return isinstance(path, str) and path.startswith("/dev/") and Path(path).is_absolute()


def _log_info(logger: Any | None, message: str) -> None:
    if logger is not None:
        logger.info(message)


def _log_warning(logger: Any | None, message: str) -> None:
    if logger is not None:
        logger.warning(message)


def _log_error(logger: Any | None, message: str) -> None:
    if logger is not None:
        logger.error(message)


def resolve_trigger_source(logger: Any | None = None) -> str:
    """Resolve origem de trigger física.

    Lê triggers.source do loader central (config.json → env GN_TRIGGER_SOURCE → 'auto').

    Valores válidos: auto, gpio, pico, both
    - auto: gpio no Raspberry Pi; pico em outros dispositivos
    """
    configured = get_effective_config().triggers.source or "auto"
    if configured not in _VALID_TRIGGER_SOURCES:
        _log_warning(
            logger,
            f"triggers.source inválido ({configured!r}); usando auto",
        )
        configured = "auto"

    if configured == "auto":
        return "gpio" if is_raspberry_pi(logger=logger) else "pico"
    return configured


def find_pico_serial_port(logger: Any | None = None) -> str | None:
    """Descobre a porta serial do Pico priorizando /dev/serial/by-id."""
    by_id_entries = sorted(glob.glob("/dev/serial/by-id/*"))
    for by_id_path in by_id_entries:
        real_path = os.path.realpath(by_id_path)
        haystack = f"{by_id_path} {real_path}".lower()
        if not any(hint in haystack for hint in _PICO_HINTS):
            continue

        _log_info(
            logger,
            f"Pico detectado via by-id: {by_id_path} -> {real_path}",
        )

        if _is_device_path(real_path):
            return real_path
        if _is_device_path(by_id_path):
            return by_id_path

    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        for device_path in sorted(glob.glob(pattern)):
            if _is_device_path(device_path):
                return device_path

    return None


def get_pico_serial_port(logger: Any | None = None) -> str | None:
    """Retorna porta serial configurada/detectada; None quando indisponível."""
    env_port = os.getenv("GN_PICO_PORT")
    if env_port:
        clean_port = env_port.strip()
        if _is_device_path(clean_port) and Path(clean_port).exists():
            _log_info(logger, f"GN_PICO_PORT definido: {clean_port}")
            return clean_port
        _log_error(
            logger,
            f"GN_PICO_PORT inválido ou indisponível: {env_port!r}",
        )
        return None

    detected = find_pico_serial_port(logger=logger)
    if detected:
        return detected

    _log_warning(logger, "Pico não detectado em /dev/serial/by-id, /dev/ttyACM* ou /dev/ttyUSB*")
    return None
