from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[3]


class Handler(BaseHTTPRequestHandler):
    ui_changes = False
    requests: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, *_args):
        return

    def do_GET(self):
        type(self).requests.append(("GET", self.path))
        if "/pulls/1/files" in self.path:
            filename = "web/src/App.tsx" if type(self).ui_changes else "docs/readme.md"
            files = [
                {
                    "filename": filename,
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
            return self.send_json(files)
        if "/pulls/1" in self.path:
            return self.send_json(
                {"title": "Example", "user": {"login": "contributor"}, "labels": []}
            )
        return self.send_json([])

    def do_POST(self):
        type(self).requests.append(("POST", self.path))
        self.send_response(204)
        self.end_headers()

    def send_json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class E2EGateConfigTests(unittest.TestCase):
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

    def run_gate(self, *, ui_changes: bool):
        Handler.ui_changes = ui_changes
        Handler.requests = []
        env = dict(os.environ)
        for key in ("E2E_UI_JUDGE_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
            env.pop(key, None)
        env.update(
            {
                "GH_TOKEN": "test-token",
                "REPO": "org/repo",
                "PR": "1",
                "GITHUB_API_URL": self.api_url,
                "MAINTAINERS": "",
            }
        )
        return subprocess.run(
            ["bash", ".github/scripts/e2e-ui-required/check.sh"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_non_ui_pr_passes_without_judge_configuration_or_request(self):
        result = self.run_gate(ui_changes=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("touches no web/** files", result.stdout)
        self.assertFalse(any(method == "POST" for method, _ in Handler.requests))

    def test_ui_pr_fails_closed_without_model_and_makes_no_judge_request(self):
        result = self.run_gate(ui_changes=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OMNIGENT_CI_E2E_JUDGE_MODEL", result.stderr)
        self.assertFalse(any(method == "POST" for method, _ in Handler.requests))


if __name__ == "__main__":
    unittest.main()
