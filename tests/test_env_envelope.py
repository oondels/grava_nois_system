"""Testes do envelope criptografado para .env admin."""

from __future__ import annotations

import json
import os
import unittest

from src.security.env_envelope import (
    derive_aes_key,
    open_env_envelope,
    seal_env_envelope,
)

DEVICE_SECRET = "test-device-secret-32-chars-long!"
REQUEST_ID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
DEVICE_ID = "device-001"
ENV_CONTENT = """# Grava Nois .env
GN_API_URL=https://api.example.com
GN_API_TOKEN=tok_abc123
DEVICE_SECRET=super-secret-value
GN_MQTT_BROKER_URL=mqtt://broker:1883
"""


class TestEnvEnvelope(unittest.TestCase):
    def test_seal_and_open_roundtrip(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext=ENV_CONTENT,
            issued_at="2026-04-13T00:00:00.000Z",
        )

        self.assertEqual(envelope["version"], "v1")
        self.assertEqual(envelope["request_id"], REQUEST_ID)
        self.assertEqual(envelope["device_id"], DEVICE_ID)
        self.assertEqual(envelope["issued_at"], "2026-04-13T00:00:00.000Z")
        self.assertTrue(len(envelope["iv"]) > 0)
        self.assertTrue(len(envelope["ciphertext"]) > 0)
        self.assertTrue(len(envelope["auth_tag"]) > 0)
        self.assertTrue(len(envelope["content_hash"]) > 0)
        self.assertTrue(len(envelope["signature"]) > 0)

        decrypted = open_env_envelope(
            device_secret=DEVICE_SECRET,
            envelope=envelope,
        )
        self.assertEqual(decrypted, ENV_CONTENT)

    def test_invalid_signature_rejected(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext=ENV_CONTENT,
        )
        tampered = dict(envelope)
        tampered["signature"] = "AAAA" + tampered["signature"][4:]

        with self.assertRaises(ValueError) as ctx:
            open_env_envelope(device_secret=DEVICE_SECRET, envelope=tampered)
        self.assertIn("inválida", str(ctx.exception))

    def test_wrong_secret_rejected(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext=ENV_CONTENT,
        )
        with self.assertRaises(ValueError) as ctx:
            open_env_envelope(
                device_secret="wrong-secret-wrong-secret-wrong!",
                envelope=envelope,
            )
        self.assertIn("inválida", str(ctx.exception))

    def test_tampered_ciphertext_rejected(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext=ENV_CONTENT,
        )
        tampered = dict(envelope)
        tampered["ciphertext"] = "AAAA" + tampered["ciphertext"][4:]

        with self.assertRaises((ValueError, Exception)):
            open_env_envelope(device_secret=DEVICE_SECRET, envelope=tampered)

    def test_invalid_version_rejected(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext=ENV_CONTENT,
        )
        tampered = dict(envelope)
        tampered["version"] = "v2"

        with self.assertRaises(ValueError) as ctx:
            open_env_envelope(device_secret=DEVICE_SECRET, envelope=tampered)
        self.assertIn("não suportada", str(ctx.exception))

    def test_hkdf_deterministic(self) -> None:
        key1 = derive_aes_key(DEVICE_SECRET, REQUEST_ID)
        key2 = derive_aes_key(DEVICE_SECRET, REQUEST_ID)
        self.assertEqual(key1, key2)

        key3 = derive_aes_key(DEVICE_SECRET, "different-request-id")
        self.assertNotEqual(key1, key3)

    def test_empty_content(self) -> None:
        envelope = seal_env_envelope(
            device_secret=DEVICE_SECRET,
            request_id=REQUEST_ID,
            device_id=DEVICE_ID,
            plaintext="",
        )
        decrypted = open_env_envelope(
            device_secret=DEVICE_SECRET,
            envelope=envelope,
        )
        self.assertEqual(decrypted, "")

    def test_hkdf_cross_compatibility_vector(self) -> None:
        """Gera vetor fixo para comparação com TypeScript."""
        import base64

        key = derive_aes_key(DEVICE_SECRET, REQUEST_ID)
        key_b64 = base64.b64encode(key).decode("ascii")

        cross_file = "/tmp/grn_envelope_cross_test_py.json"
        with open(cross_file, "w") as f:
            json.dump({"hkdf_key_b64": key_b64}, f, indent=2)

        # Se o vetor do TS existir, comparar
        ts_file = "/tmp/grn_envelope_cross_test_ts.json"
        if os.path.exists(ts_file):
            with open(ts_file) as f:
                ts_data = json.load(f)
            self.assertEqual(
                key_b64,
                ts_data["hkdf_key_b64"],
                "HKDF key deve ser idêntica entre TS e Python",
            )


if __name__ == "__main__":
    unittest.main()
