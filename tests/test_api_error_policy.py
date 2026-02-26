from __future__ import annotations

import unittest

import requests

from src.services.api_error_policy import (
    extract_api_error_from_exception,
    parse_api_error_from_response,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        return self._payload


class APIErrorPolicyTests(unittest.TestCase):
    def test_should_delete_for_hmac_signature_mismatch(self) -> None:
        response = _FakeResponse(
            401,
            {
                "success": False,
                "message": "signature_mismatch",
                "error": {"code": "UNAUTHORIZED"},
                "requestId": "req-1",
            },
        )
        info = parse_api_error_from_response(response)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertTrue(info.should_delete_local_record)

    def test_should_delete_for_forbidden_other_client_video(self) -> None:
        response = _FakeResponse(
            403,
            {
                "success": False,
                "message": "Forbidden - video does not belong to device client",
                "error": {"code": "FORBIDDEN"},
                "requestId": "req-2",
            },
        )
        info = parse_api_error_from_response(response)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertTrue(info.should_delete_local_record)

    def test_should_not_delete_for_replay_store_unavailable(self) -> None:
        response = _FakeResponse(
            503,
            {
                "success": False,
                "message": "replay_store_unavailable",
                "error": {"code": "INTERNAL_SERVER_ERROR"},
                "requestId": "req-3",
            },
        )
        info = parse_api_error_from_response(response)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertFalse(info.should_delete_local_record)

    def test_extract_from_exception_chain(self) -> None:
        response = _FakeResponse(
            401,
            {"message": "client_mismatch", "error": {"code": "FORBIDDEN"}},
        )
        http_error = requests.exceptions.HTTPError("boom")
        http_error.response = response  # type: ignore[attr-defined]
        wrapped = RuntimeError("wrapper")
        wrapped.__cause__ = http_error

        info = extract_api_error_from_exception(wrapped)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.message, "client_mismatch")
        self.assertTrue(info.should_delete_local_record)


if __name__ == "__main__":
    unittest.main()
