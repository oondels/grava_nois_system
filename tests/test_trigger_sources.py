from __future__ import annotations

from main import _serial_line_is_trigger


def test_serial_line_is_trigger_exact_match():
    assert _serial_line_is_trigger("BTN_REPLAY", "BTN_REPLAY") is True


def test_serial_line_is_trigger_ignores_case_and_spaces():
    assert _serial_line_is_trigger("  btn_replay \r", "BTN_REPLAY") is True


def test_serial_line_is_trigger_rejects_other_tokens():
    assert _serial_line_is_trigger("PING", "BTN_REPLAY") is False
