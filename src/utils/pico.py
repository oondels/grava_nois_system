from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any

_PICO_HINTS = (
    "pico",
    "rp2040",
    "raspberry",
    "board in fs mode",
)
_FALLBACK_PORT = "/dev/ttyACM0"


def _is_device_path(path: str) -> bool:
    """Retorna True para caminhos em /dev considerados válidos."""
    return isinstance(path, str) and path.startswith("/dev/") and Path(path).is_absolute()


def _log_info(logger: Any | None, message: str) -> None:
    if logger is not None:
        logger.info(message)


def _log_warning(logger: Any | None, message: str) -> None:
    if logger is not None:
        logger.warning(message)


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


def get_pico_serial_port(logger: Any | None = None) -> str:
    """Retorna porta serial configurada, detectada ou fallback padrão."""
    env_port = os.getenv("GN_PICO_PORT")
    if env_port:
        _log_info(logger, f"GN_PICO_PORT definido: {env_port}")
        return env_port

    detected = find_pico_serial_port(logger=logger)
    if detected:
        return detected

    _log_warning(logger, f"Pico não detectado, usando fallback: {_FALLBACK_PORT}")
    return _FALLBACK_PORT
