"""Exercise the exact read-only principal probe with synthetic HTTP responses."""

import importlib.util
import json
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / ".github/scripts/ci/forge_principal.py"
SPEC = importlib.util.spec_from_file_location("forge_principal", PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

ENVIRONMENT = {
    "GITHUB_SERVER_URL": "https://forge.ozba.me",
    "GITHUB_API_URL": "https://forge.ozba.me/api/v1",
    "GITHUB_REPOSITORY": "oz/omnigent",
    "GITHUB_EVENT_NAME": "pull_request",
    "PROBE_SOURCE_SHA": "a" * 40,
    "GITHUB_RUN_ID": "5435",
}


class Response:
    status = 200

    def __init__(self, body):
        self.body = body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, count):
        return self.body[:count]


class Opener:
    def __init__(self, bodies):
        self.responses = [Response(body) for body in bodies]
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.responses[len(self.requests) - 1]


class PrincipalTests(unittest.TestCase):
    def test_two_gets_preserve_normal_identity_without_inventing_bot(self):
        opener = Opener(
            [
                json.dumps(
                    {"id": 20001, "login": "synthetic-actions", "email": "ignored"}
                ).encode(),
                json.dumps({"id": 11, "full_name": "oz/omnigent", "private": True}).encode(),
            ]
        )
        result = probe.observe(probe.context(ENVIRONMENT), probe.reader("synthetic-token", opener))
        self.assertEqual(result["principal_id"], 20001)
        self.assertIsNone(result["user_type"])
        self.assertIsNone(result["is_bot"])
        self.assertEqual(result["bot_admission"], "UNQUALIFIED")
        self.assertNotIn("email", result)
        self.assertNotIn("synthetic-token", json.dumps(result))
        self.assertEqual(
            [request.full_url for request, _ in opener.requests],
            [probe.API + "/user", probe.API + "/repos/oz/omnigent"],
        )
        for request, timeout in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "token synthetic-token")
            self.assertEqual(timeout, 12)
        self.assertTrue(all(response.closed for response in opener.responses))

    def test_context_rejects_foreign_origins_repositories_and_events(self):
        for key, value in (
            ("GITHUB_SERVER_URL", "https://foreign.invalid"),
            ("GITHUB_API_URL", "https://forge.ozba.me/api/v3"),
            ("GITHUB_REPOSITORY", "other/project"),
            ("GITHUB_EVENT_NAME", "pull_request_target"),
            ("PROBE_SOURCE_SHA", "not-a-sha"),
            ("GITHUB_RUN_ID", "0"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(probe.Refused):
                    probe.context({**ENVIRONMENT, key: value})

    def test_missing_or_header_injection_token_refuses_without_requests(self):
        for token in ("", "synthetic\nextra-header", "x" * 4097):
            opener = Opener([])
            with self.assertRaises(probe.Refused):
                probe.reader(token, opener)
            self.assertEqual(opener.requests, [])

    def test_arbitrary_route_never_reaches_transport(self):
        opener = Opener([])
        with self.assertRaises(probe.Refused):
            probe.reader("synthetic", opener)("/users/another")
        self.assertEqual(opener.requests, [])

    def test_wrong_repository_refuses_after_two_reads(self):
        values = iter(
            [{"id": 20001, "login": "synthetic"}, {"id": 12, "full_name": "oz/omnigent"}]
        )
        with self.assertRaises(probe.Refused):
            probe.observe(probe.context(ENVIRONMENT), lambda path: next(values))

    def test_virtual_principal_remains_unqualified(self):
        values = iter(
            [{"id": -2, "login": "gitea-actions"}, {"id": 11, "full_name": "oz/omnigent"}]
        )
        result = probe.observe(probe.context(ENVIRONMENT), lambda path: next(values))
        self.assertEqual(result["principal_id"], -2)
        self.assertEqual(result["bot_admission"], "UNQUALIFIED")

    def test_invalid_user_refuses_before_repository_read(self):
        for user in (
            {"id": True, "login": "synthetic"},
            {"id": 20001, "login": "unsafe\nlogin"},
            [],
        ):
            calls = []

            def read(path, calls=calls, user=user):
                calls.append(path)
                return user

            with self.subTest(user=user):
                with self.assertRaises(probe.Refused):
                    probe.observe(probe.context(ENVIRONMENT), read)
                self.assertEqual(calls, ["/user"])

    def test_response_cap_closes_stream_and_refuses(self):
        opener = Opener([b" " * (probe.MAX_RESPONSE_BYTES + 1)])
        with self.assertRaises(probe.Refused):
            probe.reader("synthetic", opener)("/user")
        self.assertTrue(opener.responses[0].closed)

    def test_redirect_handler_never_forwards_credentials(self):
        self.assertIsNone(
            probe.NoRedirect().redirect_request(
                None, None, 302, "", None, "https://foreign.invalid"
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
