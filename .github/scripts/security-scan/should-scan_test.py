from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[3]


class Handler(BaseHTTPRequestHandler):
    labels: ClassVar[list[str]] = []
    author = "contributor"
    requests: ClassVar[list[str]] = []
    fail_api = False

    def log_message(self, *_args):
        return

    def do_GET(self):
        type(self).requests.append(self.path)
        if type(self).fail_api:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps(
            {
                "labels": [{"name": name} for name in type(self).labels],
                "user": {"login": type(self).author},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ShouldScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api_url = f"http://127.0.0.1:{cls.server.server_port}/api/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=3)
        cls.server.server_close()

    def run_gate(
        self, *, association="CONTRIBUTOR", maintainers="", labels=None, fail_api=False
    ):
        Handler.labels = labels or []
        Handler.author = "contributor"
        Handler.fail_api = fail_api
        Handler.requests = []
        with tempfile.NamedTemporaryFile() as output:
            env = dict(os.environ)
            env.update(
                {
                    "EVENT_NAME": "pull_request",
                    "AUTHOR_ASSOCIATION": association,
                    "PR_AUTHOR": "contributor",
                    "MAINTAINERS": maintainers,
                    "GH_TOKEN": "test-token",
                    "REPO": "org/repo",
                    "PR": "1",
                    "GITHUB_API_URL": self.api_url,
                    "GITHUB_OUTPUT": output.name,
                }
            )
            result = subprocess.run(
                ["bash", ".github/scripts/security-scan/should-scan.sh"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            output.seek(0)
            return result, output.read().decode()

    def test_untrusted_author_scans_after_api_says_no_waiver(self):
        result, output = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scan=true", output)
        self.assertEqual(len(Handler.requests), 1)

    def test_skip_label_from_api_waives_scan(self):
        result, output = self.run_gate(labels=["skip-security-scan"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "scan=false",
            output,
            f"labels={Handler.labels}, requests={Handler.requests}",
        )

    def test_private_maintainer_match_uses_api_author(self):
        result, output = self.run_gate(maintainers="CONTRIBUTOR")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scan=false", output)
        self.assertEqual(len(Handler.requests), 1)

    def test_trusted_association_skips_api(self):
        result, output = self.run_gate(association="MEMBER")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scan=false", output)
        self.assertEqual(Handler.requests, [])

    def test_api_failure_fails_closed_to_scanning(self):
        result, output = self.run_gate(fail_api=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scan=true", output)


if __name__ == "__main__":
    unittest.main()
