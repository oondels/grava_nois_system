from __future__ import annotations

import re
import unittest

from src.security.hmac import (
    hmac_sha256_base64,
    make_nonce,
    make_timestamp_sec,
    sha256_base64,
)
from src.security.request_signer import sign_request


class SecuritySigningTests(unittest.TestCase):
    def test_sha256_base64_known_value(self) -> None:
        self.assertEqual(
            sha256_base64("hello"),
            "LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ=",
        )

    def test_hmac_sha256_base64_known_value(self) -> None:
        self.assertEqual(
            hmac_sha256_base64("secret", "message"),
            "i19IcCmVwVmMVz2x4hhmqbgl1KeU0WnXBgoDYFeWNgs=",
        )

    def test_nonce_and_timestamp_format(self) -> None:
        nonce = make_nonce()
        timestamp = make_timestamp_sec()
        self.assertRegex(
            nonce,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertRegex(timestamp, r"^\d+$")

    def test_sign_request_builds_exact_canonical_string(self) -> None:
        body = '{"size_bytes":10,"sha256":"abc"}'
        signed = sign_request(
            method="POST",
            path="/api/videos/clip123/uploaded",
            body_string=body,
            device_id="device-01",
            device_secret="super-secret",
            client_id="client-01",
            timestamp="1700000000",
            nonce="11111111-2222-4333-8444-555555555555",
        )

        self.assertEqual(
            signed.canonical_string,
            "v1:POST:/api/videos/clip123/uploaded:1700000000:11111111-2222-4333-8444-555555555555:ETRLN8LtimCoU8+J5TwiJdSBAIFbw5sn18swMRTemmo=",
        )
        self.assertEqual(signed.headers["X-Device-Id"], "device-01")
        self.assertEqual(signed.headers["X-Client-Id"], "client-01")
        self.assertEqual(signed.headers["X-Timestamp"], "1700000000")
        self.assertEqual(
            signed.headers["X-Nonce"], "11111111-2222-4333-8444-555555555555"
        )
        self.assertEqual(
            signed.headers["X-Body-SHA256"],
            "ETRLN8LtimCoU8+J5TwiJdSBAIFbw5sn18swMRTemmo=",
        )

    def test_sign_request_derives_client_id_from_metadata_path(self) -> None:
        signed = sign_request(
            method="POST",
            path="/api/videos/metadados/client/client-99/venue/venue-22",
            body_string="{}",
            device_id="device-01",
            device_secret="secret",
            client_id=None,
            timestamp="1700000000",
            nonce="11111111-2222-4333-8444-555555555555",
        )
        self.assertEqual(signed.headers["X-Client-Id"], "client-99")

    def test_sign_request_requires_client_id_when_not_in_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "client_id"):
            sign_request(
                method="POST",
                path="/api/videos/clip123/uploaded",
                body_string="{}",
                device_id="device-01",
                device_secret="secret",
                client_id=None,
            )


if __name__ == "__main__":
    unittest.main()
