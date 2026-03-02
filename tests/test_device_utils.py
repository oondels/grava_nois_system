from __future__ import annotations

from src.utils import device


def test_is_raspberry_pi_detects_model(monkeypatch):
    monkeypatch.delenv("GN_FORCE_RASPBERRY_PI", raising=False)
    monkeypatch.setattr(device, "detect_raspberry_model", lambda: "Raspberry Pi 4 Model B")
    assert device.is_raspberry_pi() is True


def test_is_raspberry_pi_respects_force_override(monkeypatch):
    monkeypatch.setenv("GN_FORCE_RASPBERRY_PI", "0")
    monkeypatch.setattr(device, "detect_raspberry_model", lambda: "Raspberry Pi 4 Model B")
    assert device.is_raspberry_pi() is False


def test_detect_raspberry_model_returns_none_when_files_unavailable(monkeypatch):
    class DummyPath:
        def __init__(self, _):
            pass

        def read_text(self, **kwargs):
            raise OSError("not found")

    monkeypatch.setattr(device, "Path", DummyPath)
    assert device.detect_raspberry_model() is None
