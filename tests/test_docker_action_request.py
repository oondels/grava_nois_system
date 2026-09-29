from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.services.docker_action_request import DockerActionRequestService


class DockerActionRequestServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "docker-action.request.json"
        self.service = DockerActionRequestService(
            enabled=True,
            request_path=self.path,
            pull_token="PULL_DOCKER",
            restart_token="RESTART_DOCKER",
        )

    def request(self):
        return json.loads(next((self.service.action_root / "requests").glob("*.json")).read_text())

    def test_pico_token_is_consumed_and_creates_durable_v2_request(self):
        self.assertTrue(self.service.handle_token("pull_docker"))
        request = self.request()
        self.assertEqual(request["schema_version"], 2)
        self.assertEqual(request["source"], "pico")
        self.assertEqual(request["action"], "pull_and_recreate")
        self.assertFalse(self.path.exists())

    def test_disabled_token_consumed_but_admin_not_accepted(self):
        self.service.enabled = False
        self.assertTrue(self.service.handle_token("RESTART_DOCKER"))
        self.assertFalse(self.service.request_action("restart_container", source="mqtt"))
        self.assertEqual(
            self.service.submit_action("restart_container", source="mqtt").code, "disabled"
        )

    def test_shutdown_requires_opt_in_and_local_source(self):
        self.assertTrue(self.service.handle_token("SHUTDOWN_HOST"))
        self.assertFalse(self.service.action_root.exists())
        self.service.shutdown_enabled = True
        self.assertFalse(self.service.submit_action("shutdown_host", source="mqtt").accepted)
        self.assertTrue(self.service.handle_token("SHUTDOWN_HOST"))
        self.assertEqual(self.request()["action"], "shutdown_host")

    def test_busy_is_not_success_and_deduplication_matches_action(self):
        req = str(uuid.uuid4())
        self.assertEqual(
            self.service.submit_action("restart_container", source="mqtt", request_id=req).code,
            "created",
        )
        self.assertEqual(
            self.service.submit_action("restart_container", source="mqtt", request_id=req).code,
            "already_known",
        )
        self.assertEqual(
            self.service.submit_action("reboot_host", source="mqtt", request_id=req).code,
            "request_id_conflict",
        )
        self.assertEqual(
            self.service.submit_action("restart_container", source="mqtt").code, "busy"
        )

    def test_two_concurrent_requests_admit_at_most_one(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(
                    lambda _: self.service.submit_action("restart_container", source="mqtt"),
                    range(2),
                )
            )
        self.assertEqual(sum(r.accepted for r in results), 1)

    def test_legacy_pending_blocks_admission_without_overwriting(self):
        self.path.write_text('{"request_id":"legacy"}')
        self.assertFalse(self.service.request_action("restart_container", source="mqtt"))
        self.assertEqual(json.loads(self.path.read_text())["request_id"], "legacy")

    def test_expired_path_traversal_and_unknown_actions_rejected(self):
        expires = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.assertEqual(
            self.service.submit_action("restart_container", source="mqtt", expires_at=expires).code,
            "expired",
        )
        self.assertEqual(
            self.service.submit_action("change_wifi", source="mqtt", request_id="../../file").code,
            "invalid_request_id",
        )
        self.assertEqual(
            self.service.submit_action("arbitrary_shell", source="mqtt").code, "invalid_action"
        )
        self.assertFalse(self.service.handle_token("BTN_REPLAY"))

    def test_persistence_failure_is_not_success(self):
        with patch(
            "src.services.docker_action_request.atomic_json", side_effect=OSError("disk full")
        ):
            self.assertFalse(self.service.request_action("restart_container", source="mqtt"))
            self.assertTrue(self.service.handle_token("RESTART_DOCKER"))

    def test_wifi_secret_is_private_and_not_in_request(self):
        with patch.dict(
            "os.environ", {"GN_HOST_ACTION_SECRET_DIR": str(Path(self.tmp.name) / "secrets")}
        ):
            result = self.service.submit_action(
                "change_wifi",
                source="mqtt",
                parameters={"ssid": "test-net", "wifi_password": "fake-password"},
            )
        self.assertTrue(result.accepted)
        request = self.request()
        self.assertNotIn("fake-password", json.dumps(request))
        secret = Path(self.tmp.name) / "secrets" / request["parameters"]["secret_ref"]
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
