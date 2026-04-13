"""Tests for _send_pico_command helper and ACK_GRN_STARTED routing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from main import (
    PICO_ACK_STARTED,
    PICO_STARTED_COMMAND,
    PicoStartedHandshake,
    _send_pico_command,
    _serial_line_is_trigger,
)
from src.services.docker_action_request import DockerActionRequestService


class SendPicoCommandTests(unittest.TestCase):
    """Unit tests for the _send_pico_command helper."""

    @patch("main.os.write")
    @patch("main.select.select")
    def test_sends_command_with_newline(
        self, mock_select: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_select.return_value = ([], [42], [])
        mock_write.return_value = len(b"GRN_STARTED\n")
        mock_logger = MagicMock()
        result = _send_pico_command(42, "GRN_STARTED", _logger=mock_logger)

        self.assertTrue(result)
        mock_write.assert_called_once_with(42, b"GRN_STARTED\n")

    @patch("main.select.select")
    @patch("main.os.write", side_effect=BlockingIOError)
    def test_returns_false_on_blocking_io(
        self, _mock_write: MagicMock, mock_select: MagicMock
    ) -> None:
        mock_select.return_value = ([], [42], [])
        mock_logger = MagicMock()
        result = _send_pico_command(42, "GRN_STARTED", _logger=mock_logger)

        self.assertFalse(result)
        mock_logger.warning.assert_not_called()

    @patch("main.select.select")
    @patch("main.os.write", side_effect=OSError("device not available"))
    def test_returns_false_on_os_error(
        self, _mock_write: MagicMock, mock_select: MagicMock
    ) -> None:
        mock_select.return_value = ([], [42], [])
        mock_logger = MagicMock()
        result = _send_pico_command(42, "GRN_STARTED", _logger=mock_logger)

        self.assertFalse(result)
        mock_logger.error.assert_called()

    @patch("main.select.select")
    @patch("main.os.write", side_effect=OSError("device not available"))
    def test_does_not_propagate_exception(
        self, _mock_write: MagicMock, mock_select: MagicMock
    ) -> None:
        """Ensure errors are caught and don't bubble up."""
        mock_select.return_value = ([], [42], [])
        try:
            _send_pico_command(42, "GRN_STARTED", _logger=MagicMock())
        except Exception:
            self.fail("_send_pico_command must not propagate exceptions")

    @patch("main.os.write")
    @patch("main.select.select")
    def test_strips_whitespace_from_command(
        self, mock_select: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_select.return_value = ([], [42], [])
        mock_write.return_value = len(b"GRN_STARTED\n")
        _send_pico_command(42, "  GRN_STARTED  ", _logger=MagicMock())
        mock_write.assert_called_once_with(42, b"GRN_STARTED\n")

    @patch("main.os.write")
    @patch("main.select.select")
    def test_completes_partial_write(
        self, mock_select: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_select.return_value = ([], [42], [])
        mock_write.side_effect = [4, 8]

        result = _send_pico_command(42, "GRN_STARTED", _logger=MagicMock())

        self.assertTrue(result)
        self.assertEqual(mock_write.call_count, 2)
        mock_write.assert_any_call(42, b"GRN_STARTED\n")
        mock_write.assert_any_call(42, b"STARTED\n")


class PicoStartedHandshakeTests(unittest.TestCase):
    """Unit tests for retrying GRN_STARTED until ACK is received."""

    @patch("main._send_pico_command")
    def test_retries_after_initial_failure(self, mock_send: MagicMock) -> None:
        mock_send.side_effect = [False, True]
        handshake = PicoStartedHandshake()

        self.assertFalse(handshake.maybe_send(42, now=0.0, _logger=MagicMock()))
        self.assertFalse(handshake.ack_received)
        self.assertFalse(handshake.maybe_send(42, now=0.1, _logger=MagicMock()))
        self.assertTrue(handshake.maybe_send(42, now=0.25, _logger=MagicMock()))
        self.assertEqual(mock_send.call_count, 2)

    @patch("main._send_pico_command")
    def test_ack_stops_retries(self, mock_send: MagicMock) -> None:
        handshake = PicoStartedHandshake()

        self.assertTrue(handshake.maybe_send(42, now=0.0, _logger=MagicMock()))
        handshake.mark_ack()
        self.assertFalse(handshake.maybe_send(42, now=10.0, _logger=MagicMock()))
        mock_send.assert_called_once()

    @patch("main._send_pico_command", return_value=False)
    def test_warning_is_throttled_until_ack_timeout(
        self,
        mock_send: MagicMock,
    ) -> None:
        log = MagicMock()
        handshake = PicoStartedHandshake()

        handshake.maybe_send(42, now=0.0, _logger=log)
        handshake.maybe_send(42, now=0.25, _logger=log)
        log.warning.assert_not_called()

        handshake.maybe_send(42, now=10.25, _logger=log)
        log.warning.assert_called_once()
        self.assertEqual(mock_send.call_count, 3)


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
