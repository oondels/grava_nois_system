"""Tests for _send_pico_command helper and ACK_GRN_STARTED routing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from main import (
    PICO_ACK_STARTED,
    PICO_STARTED_COMMAND,
    _send_pico_command,
    _serial_line_is_trigger,
)
from src.services.docker_action_request import DockerActionRequestService


class SendPicoCommandTests(unittest.TestCase):
    """Unit tests for the _send_pico_command helper."""

    @patch("main.os.write")
    def test_sends_command_with_newline(self, mock_write: MagicMock) -> None:
        mock_logger = MagicMock()
        result = _send_pico_command(42, "GRN_STARTED", _logger=mock_logger)

        self.assertTrue(result)
        mock_write.assert_called_once_with(42, b"GRN_STARTED\n")
        mock_logger.info.assert_called()

    @patch("main.os.write", side_effect=BlockingIOError)
    def test_returns_false_on_blocking_io(self, _mock_write: MagicMock) -> None:
        mock_logger = MagicMock()
        result = _send_pico_command(42, "GRN_STARTED", _logger=mock_logger)

        self.assertFalse(result)
        mock_logger.warning.assert_called()

    @patch("main.os.write", side_effect=OSError("device not available"))
    def test_returns_false_on_os_error(self, _mock_write: MagicMock) -> None:
        mock_logger = MagicMock()
        result = _send_pico_command(42, "GRN_STARTED", _logger=mock_logger)

        self.assertFalse(result)
        mock_logger.error.assert_called()

    @patch("main.os.write", side_effect=OSError("device not available"))
    def test_does_not_propagate_exception(self, _mock_write: MagicMock) -> None:
        """Ensure errors are caught and don't bubble up."""
        try:
            _send_pico_command(42, "GRN_STARTED", _logger=MagicMock())
        except Exception:
            self.fail("_send_pico_command must not propagate exceptions")

    @patch("main.os.write")
    def test_strips_whitespace_from_command(self, mock_write: MagicMock) -> None:
        _send_pico_command(42, "  GRN_STARTED  ", _logger=MagicMock())
        mock_write.assert_called_once_with(42, b"GRN_STARTED\n")


class AckGrnStartedRoutingTests(unittest.TestCase):
    """ACK_GRN_STARTED must not trigger cameras, Docker actions, or warnings."""

    def test_ack_is_not_a_trigger(self) -> None:
        """ACK_GRN_STARTED does not match the global trigger token."""
        self.assertFalse(_serial_line_is_trigger(PICO_ACK_STARTED, "BTN_REPLAY"))

    def test_ack_is_not_docker_action(self) -> None:
        """ACK_GRN_STARTED is not consumed by DockerActionRequestService."""
        service = DockerActionRequestService(
            enabled=True,
            request_path=None,
            pull_token="PULL_DOCKER",
            restart_token="RESTART_DOCKER",
        )
        self.assertFalse(service.handle_token(PICO_ACK_STARTED))

    def test_ack_not_in_dedicated_token_map(self) -> None:
        """ACK_GRN_STARTED should never match a camera-dedicated token."""
        token_map = {"BTN_1": "cam01", "BTN_2": "cam02"}
        self.assertNotIn(PICO_ACK_STARTED, token_map)

    def test_constants_match_firmware(self) -> None:
        """Ensure constants align with firmware protocol."""
        self.assertEqual(PICO_STARTED_COMMAND, "GRN_STARTED")
        self.assertEqual(PICO_ACK_STARTED, "ACK_GRN_STARTED")


if __name__ == "__main__":
    unittest.main()
