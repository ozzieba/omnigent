"""Actual Gate shell sites against an owned synthetic HTTP Forge contract."""

import base64
import http.client
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / ".github/scripts/polly/forge_client.py"
WORKFLOW = ROOT / ".github/workflows/e2e-ui-required.yml"
CHECK = ROOT / ".github/scripts/e2e-ui-required/check.sh"
MAINTAINERS = ROOT / ".github/scripts/merge-ready/load-maintainers.sh"
spec = importlib.util.spec_from_file_location("gate_client", CLIENT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
HEAD, BASE = "a" * 40, "b" * 40
COMMANDS = []


def tearDownModule():
    if os.environ.get("CASE_RECEIPT"):
        Path(os.environ["CASE_RECEIPT"]).write_text(json.dumps(COMMANDS, indent=2) + "\n")


def run_block(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name)
    start = lines.index("        run: |", start) + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("      - name:")),
        len(lines),
    )
    return "\n".join(line[10:] for line in lines[start:end]) + "\n"


def file_row(name="web/a.ts", **values):
    return {"filename": name, "status": "modified", **values}


def review(ident=1, state="APPROVED", login="oz", dismissed=False):
    return {
        "id": ident,
        "state": state,
        "user": {"id": 1, "login": login},
        "dismissed": dismissed,
        "submitted_at": f"2026-10-11T00:00:{ident:02d}Z",
    }


class Wire:
    def __init__(self, files=None, reviews=None, overrides=None):
        self.calls = []
        self.files = [file_row()] if files is None else files
        self.reviews = reviews or []
        self.overrides = overrides or {}
        self.pull_count = 0
        self.drift = False
        self.pull = {
            "number": 89,
            "head": {"sha": HEAD},
            "base": {"sha": BASE},
            "changed_files": len(self.files),
            "title": "Synthetic UI change",
            "labels": [],
            "user": {"id": 22, "login": "contributor"},
        }
        self.diff = (
            "diff --git a/web/a.ts b/web/a.ts\n--- a/web/a.ts\n"
            "+++ b/web/a.ts\n@@ -1 +1 @@\n-old\n+new\n"
        )
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                parent.calls.append(("GET", self.path))
                status, value, headers = parent.response(self.path)
                is_diff = self.path.endswith(".diff")
                raw = value.encode() if is_diff else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "text/plain" if is_diff else "application/json")
                for key, val in headers.items():
                    self.send_header(key, val)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                parent.calls.append(("POST", self.path))
                self.send_error(405)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    def response(self, path):
        if path in self.overrides:
            return self.overrides[path]
        if path == "/api/v1/user":
            return 200, {"id": -2, "login": "forgejo-actions"}, {}
        if path == m.PREFIX:
            return 200, {"id": 11, "full_name": "oz/omnigent"}, {}
        if path == m.PREFIX + "/pulls/89":
            self.pull_count += 1
            value = dict(self.pull)
            if self.drift and self.pull_count > 1:
                value["head"] = {"sha": "c" * 40}
            return 200, value, {}
        if path.endswith("/contents/.github/MAINTAINER?ref=main"):
            return (
                200,
                {
                    "type": "file",
                    "path": ".github/MAINTAINER",
                    "encoding": "base64",
                    "content": base64.b64encode(b"# Synthetic fixture\noz\n").decode(),
                },
                {},
            )
        if path.endswith("/pulls/89.diff"):
            return 200, self.diff, {}
        for suffix, rows in (("files", self.files), ("reviews", self.reviews)):
            prefix = m.PREFIX + "/pulls/89/" + suffix + "?limit=50&page="
            if path.startswith(prefix):
                page = int(path[len(prefix) :])
                return (
                    200,
                    rows[(page - 1) * 50 : page * 50],
                    {"X-Total-Count": str(len(rows))},
                )
        return 404, {}, {}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            raise RuntimeError("OWN_SERVER_NOT_JOINED")

    def client(self):
        return m.ForgeClient(
            m.FORGE_ACTIONS_BINDING,
            m.Transport(
                "synthetic-token",
                connection_factory=lambda timeout: http.client.HTTPConnection(
                    "127.0.0.1", self.server.server_port, timeout=timeout
                ),
            ),
        )


class Cases(unittest.TestCase):
    def operation(self, server, *args):
        return server.client().dispatch(m.parse_operation(list(args)))

    def shell(self, server, *, legacy=False, needs_test=False, env_extra=None):
        with tempfile.TemporaryDirectory(prefix="gate-platform-") as name:
            folder = Path(name)
            bin_dir = folder / "bin"
            bin_dir.mkdir()
            for tool in (
                "cp",
                "mktemp",
                "chmod",
                "dirname",
                "base64",
                "sed",
                "paste",
                "jq",
                "tr",
                "grep",
                "head",
            ):
                (bin_dir / tool).symlink_to("/usr/bin/" + tool)
            (bin_dir / "python3").symlink_to(sys.executable)
            workspace = folder / "workspace"
            script_dir = workspace / ".github/scripts/polly"
            script_dir.mkdir(parents=True)
            source = CLIENT.read_text()
            injection = (
                "Transport.connection = staticmethod(lambda timeout: "
                f"http.client.HTTPConnection('127.0.0.1', {server.server.server_port}, "
                "timeout=timeout))\n"
            )
            if legacy:
                injection += "parse_gate_operation = lambda argv: None\n"
            source = source.replace(
                'if __name__ == "__main__":', injection + '\nif __name__ == "__main__":'
            )
            (script_dir / "forge_client.py").write_text(source)
            judge_calls = folder / "judge-calls"
            (bin_dir / "curl").write_text(
                "#!/bin/bash\n"
                'printf "called\\n" >> "$FIXTURE_JUDGE_CALLS"\n'
                'printf "%s\\n" "$FIXTURE_JUDGE_RESPONSE"\n'
            )
            (bin_dir / "curl").chmod(0o500)
            gh_path = folder / "gh-path"
            output = folder / "outputs"
            script = folder / "run.sh"
            script.write_text(
                "set -euo pipefail\n"
                + run_block("Select personal Forge platform client")
                + '\nexport PATH="$(cat "$GITHUB_PATH"):$PATH"\n'
                + f"bash '{MAINTAINERS}'\n"
                + 'export MAINTAINERS="$(sed -n \'s/^list=//p\' "$GITHUB_OUTPUT")"\n'
                + f"bash '{CHECK}'\n"
            )
            # All network paths are synthetic, and every inherited secret is excluded.
            fixture_env = {
                "HOME": os.environ["HOME"],
                "PATH": str(bin_dir),
                "LANG": "C.UTF-8",
                "GITHUB_SERVER_URL": m.ORIGIN,
                "GITHUB_REPOSITORY": m.REPOSITORY,
                "GITHUB_WORKSPACE": str(workspace),
                "RUNNER_TEMP": str(folder),
                "GITHUB_PATH": str(gh_path),
                "GITHUB_OUTPUT": str(output),
                "GH_TOKEN": "synthetic-token",
                "REPO": m.REPOSITORY,
                "PR": "89",
                "E2E_UI_JUDGE_MODEL": "synthetic-model",
                "OPENAI_BASE_URL": "https://fixture.invalid/v1",
                "OPENAI_API_KEY": "synthetic-shared-key",
                "FIXTURE_JUDGE_CALLS": str(judge_calls),
                "FIXTURE_JUDGE_RESPONSE": json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "needs_test": needs_test,
                                            "reason": "synthetic verdict",
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ),
            }
            fixture_env.update(env_extra or {})
            # Explicit bash path: the fixture PATH deliberately contains no installed gh.
            (bin_dir / "bash").symlink_to("/bin/bash")
            (bin_dir / "cat").symlink_to("/bin/cat")
            start = time.monotonic()
            command = ["/bin/bash", "--noprofile", "--norc", str(script)]
            process = subprocess.Popen(
                command,
                env=fixture_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            timed_out = False
            try:
                out, err = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                out, err = process.communicate(timeout=2)
            COMMANDS.append(
                {
                    "test": self.id(),
                    "command": command,
                    "pid": process.pid,
                    "exit_code": process.returncode,
                    "reaped": process.poll() is not None,
                    "timed_out": timed_out,
                    "duration_s": time.monotonic() - start,
                }
            )
            self.assertFalse(timed_out)
            self.assertNotIn("synthetic-token", err.decode())
            count = len(judge_calls.read_text().splitlines()) if judge_calls.exists() else 0
            return process.returncode, out.decode(), err.decode(), count

    def test_all_seven_read_sites_and_no_writes(self):
        with Wire(reviews=[review()]) as server:
            server.pull["labels"] = [{"name": "skip-e2e-ui-test"}]
            commands = [
                (
                    [
                        "api",
                        "repos/oz/omnigent/contents/.github/MAINTAINER?ref=main",
                        "--jq",
                        ".content",
                    ],
                    None,
                ),
                (
                    [
                        "api",
                        "repos/oz/omnigent/pulls/89/files",
                        "--paginate",
                        "--jq",
                        m.FILES_QUERY,
                    ],
                    "modified\tweb/a.ts\n",
                ),
                (["api", "repos/oz/omnigent/pulls/89/files", "--paginate"], None),
                (
                    [
                        "pr",
                        "view",
                        "89",
                        "--repo",
                        m.REPOSITORY,
                        "--json",
                        "title",
                        "--jq",
                        ".title",
                    ],
                    "Synthetic UI change\n",
                ),
                (
                    [
                        "pr",
                        "view",
                        "89",
                        "--repo",
                        m.REPOSITORY,
                        "--json",
                        "author",
                        "--jq",
                        ".author.login",
                    ],
                    "contributor\n",
                ),
                (
                    ["api", "repos/oz/omnigent/pulls/89", "--jq", m.LABEL_QUERY],
                    "true\n",
                ),
                (
                    [
                        "api",
                        "repos/oz/omnigent/pulls/89/reviews",
                        "--paginate",
                        "--jq",
                        m.REVIEW_QUERY,
                    ],
                    "oz\n",
                ),
            ]
            for command, expected in commands:
                result = self.operation(server, *command)
                if expected is not None:
                    self.assertEqual(result, expected)
                elif command[-1] == ".content":
                    self.assertEqual(base64.b64decode(result), b"# Synthetic fixture\noz\n")
                else:
                    self.assertIn("+new", json.loads(result)[0]["patch"])
            self.assertEqual(sum(path == "/api/v1/user" for _, path in server.calls), 7)
            self.assertTrue(all(method == "GET" for method, _ in server.calls))

    def test_parent_unsupported_gate_red_child_backend_green(self):
        with Wire(files=[file_row("docs/HOST_ADMISSION.md")]) as server:
            parent = self.shell(server, legacy=True)
            self.assertNotEqual(parent[0], 0)
            self.assertIn("UNSUPPORTED_SITE", parent[2])
            self.assertEqual(parent[3], 0)
            server.calls.clear()
            child = self.shell(
                server,
                env_extra={
                    "E2E_UI_JUDGE_MODEL": "",
                    "E2E_UI_JUDGE_BASE_URL": "https://fixture.invalid",
                },
            )
            self.assertEqual(child[0], 0, child[2])
            self.assertIn("touches no web/", child[1])
            self.assertEqual(child[3], 0)
            self.assertFalse(any(path.endswith(".diff") for _, path in server.calls))

    def test_ui_actual_shell_verdict_and_maintainer_author(self):
        with Wire() as server:
            result = self.shell(server)
            self.assertEqual(result[0], 0, result[2])
            self.assertEqual(result[3], 1)
            refused = self.shell(server, needs_test=True)
            self.assertEqual(refused[0], 1)
            server.pull["labels"] = [{"name": "skip-e2e-ui-test"}]
            server.pull["user"] = {"id": 1, "login": "oz"}
            author = self.shell(server, needs_test=True)
            self.assertEqual(author[0], 0, author[2])
            self.assertIn("author @oz", author[1])

    def test_ui_actual_shell_latest_decisive_waiver(self):
        with Wire(reviews=[review(), review(2, "COMMENT")]) as server:
            server.pull["labels"] = [{"name": "skip-e2e-ui-test"}]
            allowed = self.shell(server, needs_test=True)
            self.assertEqual(allowed[0], 0, allowed[2])
            self.assertIn("approved by maintainer @oz", allowed[1])
            server.reviews.append(review(3, "REQUEST_CHANGES"))
            denied = self.shell(server, needs_test=True)
            self.assertEqual(denied[0], 1)
            self.assertIn("not effective", denied[1])
            server.reviews = [review(dismissed=True)]
            self.assertEqual(self.shell(server, needs_test=True)[0], 1)

    def test_ui_missing_pair_or_model_never_reaches_fake_judge(self):
        for extra in (
            {"E2E_UI_JUDGE_MODEL": ""},
            {
                "E2E_UI_JUDGE_BASE_URL": "https://fixture.invalid",
                "E2E_UI_JUDGE_API_KEY": "",
            },
        ):
            with self.subTest(extra=extra), Wire() as server:
                result = self.shell(server, env_extra=extra)
                self.assertNotEqual(result[0], 0)
                self.assertEqual(result[3], 0)

    def test_each_read_checks_exact_existing_actions_actor_and_repo(self):
        for user in (
            {"id": 1, "login": "oz"},
            {"id": -2, "login": "other"},
            {"id": True, "login": "forgejo-actions"},
        ):
            with (
                self.subTest(user=user),
                Wire(overrides={"/api/v1/user": (200, user, {})}) as server,
            ):
                with self.assertRaisesRegex(m.Refusal, "ACTIONS_IDENTITY_DRIFT"):
                    self.operation(server, "api", "repos/oz/omnigent/pulls/89/files", "--paginate")
                self.assertEqual(server.calls, [("GET", "/api/v1/user")])
        with (
            Wire(overrides={m.PREFIX: (200, {"id": 12, "full_name": m.REPOSITORY}, {})}) as server,
            self.assertRaisesRegex(m.Refusal, "REPOSITORY_IDENTITY_DRIFT"),
        ):
            self.operation(server, "api", "repos/oz/omnigent/pulls/89/files", "--paginate")

    def test_complete_paginated_file_projection(self):
        files = [file_row(f"docs/f{i}") for i in range(51)]
        with Wire(files=files) as server:
            result = self.operation(
                server,
                "api",
                "repos/oz/omnigent/pulls/89/files",
                "--paginate",
                "--jq",
                m.FILES_QUERY,
            )
            self.assertEqual(len(result.splitlines()), 51)
            self.assertEqual(sum("/files?" in path for _, path in server.calls), 2)

    def test_missing_drifting_duplicate_short_and_next_totals_refuse(self):
        path = m.PREFIX + "/pulls/89/files?limit=50&page=1"
        cases = [
            ([file_row()], {}, "GATE_TOTAL_REQUIRED"),
            ([file_row()], {"X-Total-Count": "2"}, "GATE_SHORT_PAGE"),
            ([file_row(), file_row()], {"X-Total-Count": "2"}, "GATE_ROW_IDENTITY"),
            ([file_row()], {"X-Total-Count": "0"}, "GATE_OVERCOUNT"),
            ([file_row()], {"X-Total-Count": "201"}, "GATE_TOTAL_DRIFT_OR_BOUND"),
            (
                [file_row()],
                {
                    "X-Total-Count": "1",
                    "Link": '<https://fixture.invalid>; rel="prev next"',
                },
                "GATE_NEXT_AFTER_TOTAL",
            ),
        ]
        for rows, headers, error in cases:
            with (
                self.subTest(error=error),
                Wire(overrides={path: (200, rows, headers)}) as server,
                self.assertRaisesRegex(m.Refusal, error),
            ):
                self.operation(server, "api", "repos/oz/omnigent/pulls/89/files", "--paginate")
        files = [file_row(f"docs/f{i}") for i in range(51)]
        path2 = m.PREFIX + "/pulls/89/files?limit=50&page=2"
        with (
            Wire(
                files=files, overrides={path2: (200, [files[-1]], {"X-Total-Count": "52"})}
            ) as server,
            self.assertRaisesRegex(m.Refusal, "GATE_TOTAL_DRIFT_OR_BOUND"),
        ):
            self.operation(server, "api", "repos/oz/omnigent/pulls/89/files", "--paginate")

    def test_head_drift_or_incomplete_diff_refuses(self):
        with Wire() as server:
            server.drift = True
            with self.assertRaisesRegex(m.Refusal, "GATE_HEAD_BASE_DRIFT"):
                self.operation(server, "api", "repos/oz/omnigent/pulls/89/files", "--paginate")
        for diff, error in (
            ("", "DIFF_FILE_COVERAGE"),
            ("diff --git a/foreign b/foreign\n", "DIFF_SECTION_UNKNOWN"),
            (
                "diff --git a/web/a.ts b/web/a.ts\ndiff --git a/web/a.ts b/web/a.ts\n",
                "DIFF_SECTION_DUPLICATE",
            ),
        ):
            with self.subTest(error=error), Wire() as server:
                server.diff = diff
                with self.assertRaisesRegex(m.Refusal, error):
                    self.operation(server, "api", "repos/oz/omnigent/pulls/89/files", "--paginate")

    def test_real_git_quoted_paths_and_complete_native_patch_reconstruction(self):
        with tempfile.TemporaryDirectory(prefix="gate-diff-") as name:
            folder = Path(name)
            subprocess.run(["/usr/bin/git", "init", "--quiet", str(folder)], check=True, timeout=3)
            names = [
                "web/space name.ts",
                "web/λ.ts",
                "web/tab\tname.ts",
                "web/bell\a.ts",
                "tests/e2e_ui/test_public.py",
                "docs/unchanged.md",
            ]
            for path in names:
                target = folder / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("synthetic public fixture\n")
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": os.environ["HOME"],
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            }
            subprocess.run(
                ["/usr/bin/git", "-C", str(folder), "add", "--all"],
                check=True,
                timeout=3,
                env=env,
            )
            diff = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(folder),
                    "-c",
                    "core.quotePath=true",
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-color",
                ],
                capture_output=True,
                check=True,
                timeout=3,
                env=env,
            ).stdout.decode()
            rows = [file_row(path, status="added") for path in names]
            projected = m.public_file_patches(rows, diff)
            for row in projected:
                if row["filename"].startswith(("web/", "tests/e2e_ui/")):
                    self.assertIn("+synthetic public fixture", row["patch"])
                else:
                    self.assertNotIn("patch", row)

    def test_reviews_unknown_state_time_and_total_refuse(self):
        for row in (
            review(state="UNKNOWN"),
            {**review(), "submitted_at": "2026-10-11T00:00:00"},
            {**review(), "dismissed": "false"},
        ):
            with (
                self.subTest(row=row),
                Wire(reviews=[row]) as server,
                self.assertRaises(m.Refusal),
            ):
                self.operation(
                    server,
                    "api",
                    "repos/oz/omnigent/pulls/89/reviews",
                    "--paginate",
                    "--jq",
                    m.REVIEW_QUERY,
                )

    def test_contents_foreign_or_wrong_encoding_refuse(self):
        path = m.PREFIX + "/contents/.github/MAINTAINER?ref=main"
        for value in (
            {"type": "dir"},
            {"type": "file", "path": "elsewhere", "encoding": "base64", "content": ""},
        ):
            with (
                Wire(overrides={path: (200, value, {})}) as server,
                self.assertRaisesRegex(m.Refusal, "MAINTAINERS_MAIN_FILE"),
            ):
                self.operation(
                    server,
                    "api",
                    "repos/oz/omnigent/contents/.github/MAINTAINER?ref=main",
                    "--jq",
                    ".content",
                )

    def test_trusted_bootstrap_precedes_maintainers_and_github_is_excluded(self):
        text = WORKFLOW.read_text()
        self.assertLess(
            text.index("name: Check out gate scripts from main"),
            text.index("name: Select personal Forge platform client"),
        )
        self.assertLess(
            text.index("name: Select personal Forge platform client"),
            text.index("name: Load maintainers"),
        )
        self.assertIn("if: github.server_url == 'https://forge.ozba.me'", text)
        self.assertIn("ref: main", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn('cp "$source_dir/forge_client.py"', text)


if __name__ == "__main__":
    unittest.main()
