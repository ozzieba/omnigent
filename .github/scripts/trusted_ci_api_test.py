from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from urllib.request import Request

from trusted_ci_api import TrustedCIAPI, api_base


class Response:
    def __init__(self, body: object, headers: dict[str, str] | None = None) -> None:
        self.raw = json.dumps(body).encode()
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.raw


class TrustedCIAPITests(unittest.TestCase):
    def test_prefers_runner_api_url_and_supports_forge_fallback(self):
        self.assertEqual(
            api_base({"GITHUB_API_URL": "https://forge.example/api/v1"}),
            "https://forge.example/api/v1",
        )
        self.assertEqual(
            api_base({"GITHUB_SERVER_URL": "https://forge.example"}),
            "https://forge.example/api/v1",
        )
        with self.assertRaises(ValueError):
            api_base({})

    def test_request_uses_runner_api_and_token_header(self):
        seen: list[Request] = []

        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            seen.append(request)
            return Response({"ok": True})

        api = TrustedCIAPI("test-token", "https://forge.example/api/v1", opener)
        self.assertEqual(api.get("repos/o/r/pulls/1"), {"ok": True})
        self.assertEqual(seen[0].full_url, "https://forge.example/api/v1/repos/o/r/pulls/1")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer test-token")

    def test_pagination_stays_under_api_prefix_and_combines_pages(self):
        calls: list[str] = []

        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            calls.append(request.full_url)
            if "page=1" in request.full_url:
                return Response(
                    [1],
                    {"Link": '<https://forge.example/api/v1/repos/o/r/issues?page=2>; rel="next"'},
                )
            return Response([2])

        api = TrustedCIAPI("token", "https://forge.example/api/v1", opener)
        self.assertEqual(api.paginate("repos/o/r/issues?page=1"), [1, 2])
        self.assertEqual(len(calls), 2)
        with self.assertRaises(ValueError):
            api.paginate("https://attacker.example/repos/o/r/issues")

    def test_http_errors_are_reported_without_echoing_credentials(self):
        def opener(_request, timeout):
            self.assertEqual(timeout, 30)
            raise urllib.error.HTTPError(
                "https://forge.example", 403, "denied", {}, io.BytesIO(b"forbidden")
            )

        api = TrustedCIAPI("secret-marker", "https://forge.example/api/v1", opener)
        with self.assertRaisesRegex(RuntimeError, "HTTP 403") as caught:
            api.get("repos/o/r/pulls/1")
        self.assertNotIn("secret-marker", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
