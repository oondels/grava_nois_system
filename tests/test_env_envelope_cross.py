"""Committed cross-language fixtures: no external service or /tmp prerequisite."""

import json
import unittest
from pathlib import Path

from src.security.env_control import timestamp, validate_control
from src.security.env_envelope import open_env_envelope


class EnvCrossLanguageTests(unittest.TestCase):
    def test_typescript_and_python_fixtures(self):
        for path in (Path(__file__).parent / "fixtures").glob("env-control-v2-*.json"):
            with self.subTest(fixture=path.name):
                fixture = json.loads(path.read_text())
                self.assertEqual(
                    open_env_envelope(fixture["secret"], fixture["envelope"]), fixture["plaintext"]
                )
                for kind in ("request", "desired", "reported"):
                    payload = fixture[kind]
                    validate_control(
                        fixture["secret"],
                        payload,
                        payload["device_id"],
                        "env." + kind,
                        now=timestamp(fixture["now"]),
                    )
        self.assertTrue(
            (Path(__file__).parent / "fixtures/env-control-v2-typescript.json").is_file()
        )
        self.assertTrue((Path(__file__).parent / "fixtures/env-control-v2-python.json").is_file())
