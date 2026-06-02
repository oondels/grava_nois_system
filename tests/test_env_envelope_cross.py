"""Teste cruzado: gera envelope Python e abre envelope gerado pelo TS."""

from __future__ import annotations

import json
import os
import unittest

from src.security.env_envelope import open_env_envelope, seal_env_envelope

DEVICE_SECRET = "test-device-secret-32-chars-long!"
REQUEST_ID = "cross-test-fixed-request-id-0001"
DEVICE_ID = "device-cross-001"
ISSUED_AT = "2026-04-13T12:00:00.000Z"
ENV_CONTENT = "KEY_A=value_a\nKEY_B=value_b\nSECRET=s3cr3t\n"

PY_ENVELOPE_FILE = "/tmp/grn_cross_envelope_py.json"
TS_ENVELOPE_FILE = "/tmp/grn_cross_envelope_ts.json"


class TestEnvEnvelopeCross(unittest.TestCase):
    def test_generate_python_envelope(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext=ENV_CONTENT,
            issued_at=ISSUED_AT,
        )
        with open(PY_ENVELOPE_FILE, "w") as f:
            json.dump(envelope, f, indent=2)

        # Roundtrip próprio
        decrypted = open_env_envelope(
            device_secret=DEVICE_SECRET, envelope=envelope
        )
        self.assertEqual(decrypted, ENV_CONTENT)

    def test_open_typescript_envelope(self) -> None:
        if not os.path.exists(TS_ENVELOPE_FILE):
            self.skipTest(
                f"{TS_ENVELOPE_FILE} não existe. Execute o teste TS primeiro."
            )

        with open(TS_ENVELOPE_FILE) as f:
            ts_envelope = json.load(f)

        decrypted = open_env_envelope(
            device_secret=DEVICE_SECRET, envelope=ts_envelope
        )
        self.assertEqual(decrypted, ENV_CONTENT)


if __name__ == "__main__":
    unittest.main()
