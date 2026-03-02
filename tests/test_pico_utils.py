from __future__ import annotations

from src.utils import pico


def test_get_pico_serial_port_uses_env_override(monkeypatch):
    monkeypatch.setenv("GN_PICO_PORT", "/dev/serial/by-id/usb-Raspberry_Pico")
    monkeypatch.setattr(pico.Path, "exists", lambda self: True)
    assert pico.get_pico_serial_port() == "/dev/serial/by-id/usb-Raspberry_Pico"


def test_get_pico_serial_port_returns_none_for_invalid_env_path(monkeypatch):
    monkeypatch.setenv("GN_PICO_PORT", "/dev/serial/by-id/usb-Raspberry_Pico")
    monkeypatch.setattr(pico.Path, "exists", lambda self: False)
    assert pico.get_pico_serial_port() is None


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


def test_find_returns_none_and_get_returns_none(monkeypatch):
    monkeypatch.delenv("GN_PICO_PORT", raising=False)
    monkeypatch.delenv("GN_TRIGGER_SOURCE", raising=False)
    monkeypatch.delenv("GN_FORCE_RASPBERRY_PI", raising=False)
    monkeypatch.setattr(pico.glob, "glob", lambda _: [])

    assert pico.find_pico_serial_port() is None
    assert pico.get_pico_serial_port() is None


def test_resolve_trigger_source_auto_uses_gpio_on_raspberry(monkeypatch):
    monkeypatch.setenv("GN_TRIGGER_SOURCE", "auto")
    monkeypatch.setattr(pico, "is_raspberry_pi", lambda logger=None: True)
    assert pico.resolve_trigger_source() == "gpio"


def test_resolve_trigger_source_auto_uses_pico_off_raspberry(monkeypatch):
    monkeypatch.setenv("GN_TRIGGER_SOURCE", "auto")
    monkeypatch.setattr(pico, "is_raspberry_pi", lambda logger=None: False)
    assert pico.resolve_trigger_source() == "pico"


def test_resolve_trigger_source_invalid_value_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("GN_TRIGGER_SOURCE", "invalid")
    monkeypatch.setattr(pico, "is_raspberry_pi", lambda logger=None: False)
    assert pico.resolve_trigger_source() == "pico"
