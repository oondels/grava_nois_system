from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.docker_action_request import DockerActionRequestService


class DockerActionRequestServiceTests(unittest.TestCase):
    def test_pull_token_writes_request_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / "docker-action.request.json"
            service = DockerActionRequestService(
                enabled=True,
                request_path=request_path,
                pull_token="PULL_DOCKER",
                restart_token="RESTART_DOCKER",
            )

            handled = service.handle_token("pull_docker")

            self.assertTrue(handled)
            payload = json.loads(request_path.read_text())
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["action"], "pull_and_recreate")
            self.assertEqual(payload["source"], "pico")
            self.assertEqual(payload["token"], "PULL_DOCKER")

    def test_restart_token_writes_request_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / "docker-action.request.json"
            service = DockerActionRequestService(
                enabled=True,
                request_path=request_path,
                pull_token="PULL_DOCKER",
                restart_token="RESTART_DOCKER",
            )

            handled = service.handle_token("RESTART_DOCKER")

            self.assertTrue(handled)
            payload = json.loads(request_path.read_text())
            self.assertEqual(payload["action"], "restart_container")
            self.assertEqual(payload["source"], "pico")

    def test_admin_env_restart_writes_request_file_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / "docker-action.request.json"
            service = DockerActionRequestService(
                enabled=True,
                request_path=request_path,
                pull_token="PULL_DOCKER",
                restart_token="RESTART_DOCKER",
            )

            handled = service.request_action("restart_container", source="admin_env")

            self.assertTrue(handled)
            payload = json.loads(request_path.read_text())
            self.assertEqual(payload["action"], "restart_container")
            self.assertEqual(payload["source"], "admin_env")
            self.assertNotIn("token", payload)

    def test_disabled_matching_token_is_consumed_without_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / "docker-action.request.json"
            service = DockerActionRequestService(
                enabled=False,
                request_path=request_path,
                pull_token="PULL_DOCKER",
                restart_token="RESTART_DOCKER",
            )

            handled = service.handle_token("PULL_DOCKER")

            self.assertTrue(handled)
            self.assertFalse(request_path.exists())

    def test_unknown_token_is_not_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = DockerActionRequestService(
                enabled=True,
                request_path=Path(tmp) / "docker-action.request.json",
                pull_token="PULL_DOCKER",
                restart_token="RESTART_DOCKER",
            )

            self.assertFalse(service.handle_token("BTN_REPLAY"))

    def test_write_failure_is_consumed_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / "docker-action.request.json"
            service = DockerActionRequestService(
                enabled=True,
                request_path=request_path,
                pull_token="PULL_DOCKER",
                restart_token="RESTART_DOCKER",
            )

            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                handled = service.handle_token("PULL_DOCKER")

            self.assertTrue(handled)
            self.assertFalse(request_path.exists())


if __name__ == "__main__":
    unittest.main()
