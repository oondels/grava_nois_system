from __future__ import annotations

from src.utils import pico


def test_get_pico_serial_port_uses_env_override(monkeypatch):
    monkeypatch.setenv("GN_PICO_PORT", "/dev/serial/by-id/usb-Raspberry_Pico")
    assert pico.get_pico_serial_port() == "/dev/serial/by-id/usb-Raspberry_Pico"


def test_find_pico_serial_port_prefers_by_id_match(monkeypatch):
    monkeypatch.delenv("GN_PICO_PORT", raising=False)

    def fake_glob(pattern: str):
        if pattern == "/dev/serial/by-id/*":
            return ["/dev/serial/by-id/usb-Raspberry_Pico_123"]
        return []

    monkeypatch.setattr(pico.glob, "glob", fake_glob)
    monkeypatch.setattr(
        pico.os.path, "realpath", lambda _: "/dev/ttyACM7"
    )

    assert pico.find_pico_serial_port() == "/dev/ttyACM7"


def test_find_pico_serial_port_falls_back_to_ttyacm(monkeypatch):
    monkeypatch.delenv("GN_PICO_PORT", raising=False)

    def fake_glob(pattern: str):
        if pattern == "/dev/serial/by-id/*":
            return []
        if pattern == "/dev/ttyACM*":
            return ["/dev/ttyACM0"]
        if pattern == "/dev/ttyUSB*":
            return []
        return []

    monkeypatch.setattr(pico.glob, "glob", fake_glob)

    assert pico.find_pico_serial_port() == "/dev/ttyACM0"


def test_find_returns_none_and_get_uses_fallback(monkeypatch):
    monkeypatch.delenv("GN_PICO_PORT", raising=False)
    monkeypatch.setattr(pico.glob, "glob", lambda _: [])

    assert pico.find_pico_serial_port() is None
    assert pico.get_pico_serial_port() == "/dev/ttyACM0"
