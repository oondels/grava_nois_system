from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from src.cli.rental_compat_probe import (
    PROBE_SCHEMA_VERSION,
    RENTAL_IDENTITY_CONTRACT,
    build_probe_result,
)


class RentalCompatibilityProbeTests(unittest.TestCase):
    def test_reports_tenantless_contract_and_agent_version(self) -> None:
        result = build_probe_result({"GN_AGENT_VERSION": "2.4.1-rental"})

        self.assertTrue(result["compatible"])
        self.assertEqual(PROBE_SCHEMA_VERSION, result["probe_schema_version"])
        self.assertEqual(RENTAL_IDENTITY_CONTRACT, result["rental_identity_contract"])
        self.assertEqual("2.4.1-rental", result["agent_version"])

    def test_module_cli_outputs_machine_readable_json(self) -> None:
        env = dict(os.environ)
        env["GN_AGENT_VERSION"] = "2.4.1-rental"
        completed = subprocess.run(
            [sys.executable, "-m", "src.cli.rental_compat_probe"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        self.assertEqual("2.4.1-rental", json.loads(completed.stdout)["agent_version"])


if __name__ == "__main__":
    unittest.main()
