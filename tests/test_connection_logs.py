"""Tests: connection logs include camera_id for each camera (PRD §12)."""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

from src.video.capture import check_rtsp_connectivity


def _make_socket(connected: bool):
    """Return a mock socket that either connects or raises."""
    sock = MagicMock()
    if not connected:
        sock.connect.side_effect = socket.error("refused")
    return sock


class TestConnectionLogsWithCameraId:
    """check_rtsp_connectivity must include camera_id in every log message."""

    RTSP = "rtsp://user:pass@192.168.1.10:554/stream"

    def _run(self, connected: bool, camera_id: str = "cam01", max_retries: int = 1):
        with patch("src.video.capture.socket.socket") as mock_sock_cls, \
             patch("src.video.capture.logger") as mock_log, \
             patch("src.video.capture.time.sleep"):
            mock_sock_cls.return_value = _make_socket(connected)
            result = check_rtsp_connectivity(
                self.RTSP, timeout=1, max_retries=max_retries, camera_id=camera_id
            )
        return result, mock_log

    def test_success_log_includes_camera_id(self):
        result, mock_log = self._run(connected=True, camera_id="cam01")

        assert result is True
        all_calls = " ".join(str(c) for c in mock_log.info.call_args_list)
        assert "cam01" in all_calls

    def test_failure_log_includes_camera_id(self):
        result, mock_log = self._run(connected=False, camera_id="cam02", max_retries=1)

        assert result is False
        all_calls = " ".join(
            str(c)
            for c in mock_log.info.call_args_list
            + mock_log.warning.call_args_list
            + mock_log.error.call_args_list
        )
        assert "cam02" in all_calls

    def test_different_cameras_logged_independently(self):
        """Distinct camera_ids appear in their respective log calls."""
        logs_per_cam: dict[str, list[str]] = {}

        for cam_id in ("cam_norte", "cam_sul"):
            with patch("src.video.capture.socket.socket") as mock_sock_cls, \
                 patch("src.video.capture.logger") as mock_log, \
                 patch("src.video.capture.time.sleep"):
                mock_sock_cls.return_value = _make_socket(True)
                check_rtsp_connectivity(
                    self.RTSP, timeout=1, max_retries=1, camera_id=cam_id
                )
            logs_per_cam[cam_id] = [str(c) for c in mock_log.info.call_args_list]

        assert any("cam_norte" in m for m in logs_per_cam["cam_norte"])
        assert any("cam_sul" in m for m in logs_per_cam["cam_sul"])
        # Cross-contamination: cam_sul must NOT appear in cam_norte's logs
        assert not any("cam_sul" in m for m in logs_per_cam["cam_norte"])

    def test_no_camera_id_still_works(self):
        """Backward compat: camera_id defaults to '' (no prefix in logs)."""
        with patch("src.video.capture.socket.socket") as mock_sock_cls, \
             patch("src.video.capture.logger"), \
             patch("src.video.capture.time.sleep"):
            mock_sock_cls.return_value = _make_socket(True)
            result = check_rtsp_connectivity(self.RTSP, timeout=1, max_retries=1)
        assert result is True

    def test_invalid_url_logs_camera_id(self):
        """Even for invalid URLs the camera_id should appear in the error log."""
        with patch("src.video.capture.logger") as mock_log:
            result = check_rtsp_connectivity(
                "rtsp://", timeout=1, max_retries=1, camera_id="cam_bad"
            )
        assert result is False
        error_calls = " ".join(str(c) for c in mock_log.error.call_args_list)
        assert "cam_bad" in error_calls
