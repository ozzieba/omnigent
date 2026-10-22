"""Real owned loopback HTTP plus the maintained Polly shell blocks."""

import ast
import hashlib
import http.client
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

P = Path(__file__).resolve().parents[2]
CLIENT = P / ".github/scripts/polly/forge_client.py"
CANDIDATE = P / ".github/workflows/polly-review.yml"
spec = importlib.util.spec_from_file_location("client", CLIENT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
HEAD = "aa74c0ec4a26ce39d4fccb148a04052207596210"
BOT = -2
BINDING = {
    "actor_id": BOT,
    "actor_login": "forgejo-actions",
    "receipt_sha256": "f" * 64,
    "repo_id": 11,
    "repository": "oz/omnigent",
}
COMMANDS = []


def source_with_binding(source, binding):
    assignments = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "FORGE_ACTIONS_BINDING"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    node = assignments[0]
    lines = source.splitlines(keepends=True)
    return "".join(
        [
            *lines[: node.lineno - 1],
            f"FORGE_ACTIONS_BINDING = {binding!r}\n",
            *lines[node.end_lineno :],
        ]
    )


def block(text, name):
    lines = text.splitlines()
    start = lines.index("      - name: " + name)
    run = next(i for i in range(start, len(lines)) if lines[i] == "        run: |") + 1
    end = next(
        (i for i in range(run, len(lines)) if lines[i].startswith("      - name:")),
        len(lines),
    )
    return (
        "\n".join(
            line[10:] if line.startswith("          ") else line for line in lines[run:end]
        ).rstrip()
        + "\n"
    )


def comment(identifier=1, actor=BOT, login="forgejo-actions", marker="reviewed", **extra):
    return {
        "id": identifier,
        "user": {"id": actor, "login": login, **extra},
        "html_url": "https://forge.ozba.me/oz/omnigent/pulls/89#issuecomment-" + str(identifier),
        "body": "<!-- polly-" + marker + "-sha: " + HEAD + " -->",
    }


class FakeHTTP:
    def __init__(self, overrides=None, pages=None, totals=None, headers=None, slow_headers=False):
        self.calls = []
        self.overrides = overrides or {}
        self.pages = pages or [[]]
        self.totals = totals
        self.headers = headers or {}
        self.slow_headers = slow_headers
        self.pull = {
            "number": 89,
            "head": {"sha": HEAD, "ref": "fixture-head"},
            "base": {"ref": "main"},
            "title": "synthetic title",
            "body": "full fixture body\n\u03bb",
            "additions": 1,
            "deletions": 2,
            "changed_files": 3,
        }
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.answer()

            def do_POST(self):
                self.answer()

            def answer(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parent.calls.append(
                    (
                        self.command,
                        self.path,
                        json.loads(body) if body else None,
                        self.headers.get("Authorization"),
                    )
                )
                status, value, headers = parent.response(self.command, self.path)
                text = self.path.endswith(".diff")
                raw = (
                    value
                    if type(value) is bytes
                    else (
                        value.encode() if text else json.dumps(value, ensure_ascii=False).encode()
                    )
                )
                if parent.slow_headers:
                    try:
                        self.wfile.write(("HTTP/1.1 " + str(status) + " fixture\r\n").encode())
                        for index in range(15):
                            self.wfile.write(("X-Fixture-" + str(index) + ": x\r\n").encode())
                            time.sleep(0.01)
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True
                        return
                else:
                    self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "text/plain" if text else "application/json; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(raw)))
                for key, val in headers.items():
                    self.send_header(key, val)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = False

            def handle_error(self, _request, _client_address):
                if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                    parent.server_errors += 1
                else:
                    parent.unclassified_server_errors += 1

        self.server_errors = 0
        self.unclassified_server_errors = 0
        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    def response(self, method, path):
        if (method, path) in self.overrides:
            return self.overrides[method, path]
        if path == "/api/v1/user":
            return 200, {"id": BOT, "login": "forgejo-actions"}, {}
        if path == m.PREFIX:
            return 200, {"id": 11, "full_name": "oz/omnigent"}, {}
        if path.endswith("/pulls/89"):
            return 200, self.pull, {}
        if path.endswith("/pulls/89.diff"):
            return 200, "diff --git a/x b/x\n+full synthetic diff\n", {}
        if "/comments?" in path:
            page = int(path.rsplit("page=", 1)[1])
            rows = self.pages[page - 1] if page <= len(self.pages) else []
            headers = dict(self.headers.get(page, {}))
            total = sum(map(len, self.pages)) if self.totals is None else self.totals[page - 1]
            if total is not False:
                headers["X-Total-Count"] = str(total)
            return 200, rows, headers
        if method == "POST":
            return 201, {"id": 900, "body": "synthetic"}, {}
        return 404, {}, {}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            raise RuntimeError("FIXTURE_SERVER_NOT_JOINED")
        if self.unclassified_server_errors:
            raise RuntimeError("FIXTURE_UNCLASSIFIED_SERVER_ERROR")

    def transport(self, clock=time.monotonic):
        return m.Transport(
            "synthetic-actions-token",
            connection_factory=lambda timeout: http.client.HTTPConnection(
                "127.0.0.1", self.server.server_port, timeout=timeout
            ),
            clock=clock,
        )


HARNESS = """import http.client,importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location('client',os.environ['FIXTURE_CLIENT'])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.FORGE_ACTIONS_BINDING=json.loads(os.environ['FIXTURE_BINDING'])
m.Transport.connection=staticmethod(
    lambda timeout:http.client.HTTPConnection(
        '127.0.0.1',int(os.environ['FIXTURE_PORT']),timeout=timeout))
raise SystemExit(m.main(sys.argv[1:]))
"""


class Cases(unittest.TestCase):
    def shell(
        self,
        script,
        server=None,
        adapter=True,
        binding=BINDING,
        bootstrap_binding=False,
        bootstrap_unbound=False,
        **values,
    ):
        with tempfile.TemporaryDirectory(prefix="polly-http-") as name:
            folder = Path(name)
            bin_dir = folder / "bin"
            bin_dir.mkdir()
            for tool in (
                "grep",
                "tail",
                "cut",
                "head",
                "cat",
                "dirname",
                "chmod",
                "mktemp",
                "cp",
            ):
                (bin_dir / tool).symlink_to("/usr/bin/" + tool)
            (bin_dir / "python3").symlink_to(sys.executable)
            harness = folder / "harness.py"
            harness.write_text(HARNESS)
            if adapter:
                (bin_dir / "gh").write_text(
                    '#!/bin/bash\nexec "$FIXTURE_PYTHON" -B "$FIXTURE_HARNESS" "$@"\n'
                )
                (bin_dir / "gh").chmod(0o500)
            output = folder / "outputs"
            output.touch()
            gh_path = folder / "github_path"
            gh_path.touch()
            script_file = folder / "run.sh"
            script_file.write_text(script.replace("/tmp/", str(folder) + "/"))
            workspace = folder / "workspace/.polly-platform-source/.github/scripts/polly"
            workspace.mkdir(parents=True)
            client_source = CLIENT.read_text()
            if bootstrap_unbound:
                client_source = source_with_binding(client_source, None)
            if bootstrap_binding:
                client_source = source_with_binding(client_source, binding)
                fixture_connection = (
                    "Transport.connection = staticmethod(lambda timeout: "
                    'http.client.HTTPConnection("127.0.0.1", '
                    + str(server.server.server_port)
                    + ", timeout=timeout))\n"
                )
                client_source = client_source.replace(
                    'if __name__ == "__main__":',
                    fixture_connection + '\nif __name__ == "__main__":',
                )
            (workspace / "forge_client.py").write_text(client_source)
            env = {
                "HOME": os.environ["HOME"],
                "PATH": str(bin_dir),
                "LANG": "C.UTF-8",
                "REPO": "oz/omnigent",
                "PR_NUMBER": "89",
                "COMMENT_BODY": "",
                "COMMENT_ID": "12",
                "ISSUE_NUMBER": "89",
                "GITHUB_OUTPUT": str(output),
                "GITHUB_PATH": str(gh_path),
                "GITHUB_WORKSPACE": str(folder / "workspace"),
                "RUNNER_TEMP": str(folder),
                "GITHUB_SERVER_URL": m.ORIGIN,
                "GITHUB_REPOSITORY": m.REPOSITORY,
                "GH_TOKEN": "synthetic-actions-token",
                "REVIEW_TEXT": "synthetic final review\n\u03bb",
                "HEAD_SHA": HEAD,
                "RUN_URL": m.ORIGIN + "/oz/omnigent/actions/runs/1",
                "FIXTURE_PYTHON": sys.executable,
                "FIXTURE_HARNESS": str(harness),
                "FIXTURE_CLIENT": str(CLIENT),
                "FIXTURE_BINDING": json.dumps(binding),
                "FIXTURE_PORT": str(server.server.server_port if server else 0),
            }
            env.update(values)
            command = ["/bin/bash", "--noprofile", "--norc", str(script_file)]
            start = time.monotonic()
            process = subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            timed_out = False
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=2)
            COMMANDS.append(
                {
                    "test": self.id(),
                    "command": command,
                    "pid": process.pid,
                    "exit_code": process.returncode,
                    "reaped": process.poll() is not None,
                    "timeout": timed_out,
                    "duration_seconds": time.monotonic() - start,
                    "stdout_bytes": len(stdout),
                    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                    "stderr_bytes": len(stderr),
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                    "loopback_fake_http_only": True,
                }
            )
            self.assertFalse(timed_out)
            return (
                process.returncode,
                output.read_text(),
                stdout.decode(),
                stderr.decode(),
                gh_path.read_text(),
            )

    def dispatch(self, server, argv, binding=BINDING):
        return m.ForgeClient(binding, server.transport()).dispatch(m.parse_operation(argv))

    def test_missing_cli_red_adapter_green_actual_early_shell(self):
        early = block(CANDIDATE.read_text(), "Skip if this commit already has a review")
        with FakeHTTP() as server:
            parent = self.shell(early, server, adapter=False)
            self.assertEqual(parent[0], 127)
            self.assertEqual(server.calls, [])
            child = self.shell(early, server)
            self.assertEqual(child[0], 0, child[3])
            self.assertEqual(child[1], "head_sha=" + HEAD + "\n")
            self.assertEqual(
                [r[1] for r in server.calls],
                [
                    "/api/v1/user",
                    m.PREFIX,
                    m.PREFIX + "/pulls/89",
                    "/api/v1/user",
                    m.PREFIX,
                    m.PREFIX + "/issues/89/comments?limit=50&page=1",
                ],
            )

    def test_all_eight_actual_source_sites_full_http(self):
        workflow = CANDIDATE.read_text()
        with FakeHTTP(pages=[[comment()]]) as server:
            result = self.shell(
                block(workflow, "Validate /review command"),
                server,
                COMMENT_BODY="/review",
            )
            self.assertEqual(result[0], 0, result[3])
            self.assertIn("skip=false", result[1])
            result = self.shell(
                block(workflow, "Skip if this commit already has a review"), server
            )
            self.assertEqual(result[0], 0, result[3])
            self.assertIn("duplicate=true", result[1])
            result = self.shell(block(CANDIDATE.read_text(), "Collect PR context"), server)
            self.assertEqual(result[0], 0, result[3])
            result = self.shell(block(workflow, "Post review comment"), server)
            self.assertEqual(result[0], 0, result[3])
            operations = [r[:3] for r in server.calls if r[1] not in ("/api/v1/user", m.PREFIX)]
            self.assertEqual(len(operations), 8)
            self.assertEqual(
                operations[0],
                (
                    "POST",
                    m.PREFIX + "/issues/comments/12/reactions",
                    {"content": "eyes"},
                ),
            )
            self.assertEqual(operations[1][1], m.PREFIX + "/pulls/89")
            self.assertTrue(operations[2][1].endswith("/comments?limit=50&page=1"))
            self.assertIn("polly-skipped-sha: " + HEAD, operations[3][2]["body"])
            self.assertEqual(operations[4][1], m.PREFIX + "/pulls/89.diff")
            self.assertEqual(operations[5][1], m.PREFIX + "/pulls/89")
            self.assertEqual(operations[6][1], m.PREFIX + "/pulls/89")
            self.assertIn("synthetic final review\n\u03bb", operations[7][2]["body"])

    def test_unbound_refuses_before_network_including_force(self):
        with FakeHTTP() as server:
            result = self.shell(
                block(CANDIDATE.read_text(), "Skip if this commit already has a review"),
                server,
                binding=None,
                COMMENT_BODY="/review force",
            )
            self.assertEqual(result[0], 2)
            self.assertIn("ACTIONS_IDENTITY_UNBOUND", result[3])
            self.assertEqual(server.calls, [])

    def test_both_markers_ignore_human_name_type_and_is_bot(self):
        for marker in ("reviewed", "skipped"):
            with (
                self.subTest(marker=marker),
                FakeHTTP(
                    pages=[
                        [
                            comment(
                                actor=1,
                                login="forgejo-actions",
                                marker=marker,
                                type="Bot",
                                is_bot=True,
                            )
                        ]
                    ]
                ) as server,
            ):
                result = self.shell(
                    block(
                        CANDIDATE.read_text(),
                        "Skip if this commit already has a review",
                    ),
                    server,
                )
                self.assertEqual(result[0], 0, result[3])
                self.assertNotIn("duplicate=true", result[1])
                self.assertFalse(any(r[0] == "POST" for r in server.calls))

    def test_actions_comment_without_invented_fields_skips_before_provider(self):
        early = block(CANDIDATE.read_text(), "Skip if this commit already has a review")
        for marker, expected_posts in (("skipped", 0), ("reviewed", 1)):
            with (
                self.subTest(marker=marker),
                FakeHTTP(pages=[[comment(marker=marker)]]) as server,
            ):
                result = self.shell(
                    "(\n" + early + ')\nif ! grep -q "duplicate=true" "$GITHUB_OUTPUT"; then '
                    'printf "provider-eligible\\n"; fi\n',
                    server,
                )
                self.assertEqual(result[0], 0)
                self.assertIn("duplicate=true", result[1])
                self.assertNotIn("provider-eligible", result[2])
                self.assertEqual(sum(r[0] == "POST" for r in server.calls), expected_posts)
        gate = "if: steps.trigger.outputs.skip != 'true' && steps.dupe.outputs.duplicate != 'true'"
        self.assertIn(gate, CANDIDATE.read_text())

    def test_force_has_only_head_operation(self):
        with FakeHTTP(pages=[[comment()]]) as server:
            result = self.shell(
                block(CANDIDATE.read_text(), "Skip if this commit already has a review"),
                server,
                COMMENT_BODY="/review force",
            )
            self.assertEqual(result[0], 0, result[3])
            self.assertEqual(len(server.calls), 3)
            self.assertEqual(server.calls[-1][1], m.PREFIX + "/pulls/89")

    def test_deny_skip_notice_still_duplicate_no_retry(self):
        overrides = {("POST", m.PREFIX + "/issues/89/comments"): (403, {}, {})}
        with FakeHTTP(overrides=overrides, pages=[[comment()]]) as server:
            result = self.shell(
                block(CANDIDATE.read_text(), "Skip if this commit already has a review"),
                server,
            )
            self.assertEqual(result[0], 0)
            self.assertIn("duplicate=true", result[1])
            self.assertEqual(sum(r[0] == "POST" for r in server.calls), 1)

    def test_bound_identity_drift_refuses_without_repository_operation(self):
        for drift in (
            {"id": 1, "login": "forgejo-actions"},
            {"id": BOT, "login": "different"},
            {"id": True, "login": "forgejo-actions"},
        ):
            with (
                self.subTest(drift=drift),
                FakeHTTP(overrides={("GET", "/api/v1/user"): (200, drift, {})}) as server,
            ):
                with self.assertRaisesRegex(m.Refusal, "ACTIONS_IDENTITY_DRIFT"):
                    self.dispatch(
                        server,
                        ["api", "repos/oz/omnigent/pulls/89", "--jq", ".head.sha"],
                    )
                self.assertEqual(len(server.calls), 1)

    def test_repository_id_and_name_drift_refuse(self):
        for repo in (
            {"id": 12, "full_name": "oz/omnigent"},
            {"id": 11, "full_name": "elsewhere/omnigent"},
        ):
            with (
                self.subTest(repo=repo),
                FakeHTTP(overrides={("GET", m.PREFIX): (200, repo, {})}) as server,
            ):
                with self.assertRaisesRegex(m.Refusal, "REPOSITORY_IDENTITY_DRIFT"):
                    self.dispatch(
                        server,
                        ["api", "repos/oz/omnigent/pulls/89", "--jq", ".head.sha"],
                    )
                self.assertEqual(len(server.calls), 2)

    def test_redirect_never_follows_and_never_forwards_token(self):
        with FakeHTTP(
            overrides={
                ("GET", "/api/v1/user"): (
                    302,
                    {},
                    {"Location": "http://127.0.0.1:1/steal"},
                )
            }
        ) as server:
            with self.assertRaisesRegex(m.Refusal, "HTTP_REFUSED"):
                self.dispatch(server, ["api", "repos/oz/omnigent/pulls/89", "--jq", ".head.sha"])
            self.assertEqual(len(server.calls), 1)

    def test_two_pages_complete_without_truncating_comment_json(self):
        first = [
            comment(identifier=i, actor=1, login="human", marker="unrelated") for i in range(1, 51)
        ]
        last = comment(identifier=51)
        last["body"] = "prefix\n" * 1000 + last["body"]
        with FakeHTTP(pages=[first, [last]]) as server:
            result = self.shell(
                block(CANDIDATE.read_text(), "Skip if this commit already has a review"),
                server,
            )
            self.assertEqual(result[0], 0, result[3])
            self.assertIn("duplicate=true", result[1])
            self.assertEqual(sum("/comments?" in r[1] for r in server.calls), 2)

    def test_bad_pagination_refuses_before_post(self):
        first = [comment(identifier=i, actor=1, login="human") for i in range(1, 51)]
        pages = [
            [comment(identifier=offset + i, actor=1, login="human") for i in range(1, 51)]
            for offset in (0, 50, 100, 150)
        ]
        cases = [
            {"pages": [first, [comment(identifier=51)]], "totals": [51, 52]},
            {"pages": [first, [comment(identifier=51)]], "totals": [51, False]},
            {"pages": [first, [comment(identifier=51)]], "totals": [False, 51]},
            {"pages": [[comment()]], "totals": [2]},
            {"pages": [[comment(), comment()]]},
            {"pages": [[comment()]], "totals": [0]},
            {
                "pages": [[comment()]],
                "headers": {1: {"Link": '<https://forge.ozba.me/?page=2>; rel="next"'}},
            },
            {
                "pages": [[comment()]],
                "headers": {1: {"Link": '<https://forge.ozba.me/?page=2>; rel="prev next"'}},
            },
            {"pages": [[comment()]], "totals": [201]},
            {"pages": pages, "totals": [False] * 4},
        ]
        for case in cases:
            with self.subTest(case=str(list(case))), FakeHTTP(**case) as server:
                result = self.shell(
                    block(
                        CANDIDATE.read_text(),
                        "Skip if this commit already has a review",
                    ),
                    server,
                )
                self.assertEqual(result[0], 2)
                self.assertNotIn("duplicate=true", result[1])
                self.assertFalse(any(r[0] == "POST" for r in server.calls))

    def test_header_absent_complete_short_page_is_bounded(self):
        with FakeHTTP(pages=[[comment()]], totals=[False]) as server:
            result = self.shell(
                block(CANDIDATE.read_text(), "Skip if this commit already has a review"),
                server,
            )
            self.assertEqual(result[0], 0, result[3])
            self.assertIn("duplicate=true", result[1])

    def test_full_unicode_body_file_is_one_json_request(self):
        with (
            tempfile.TemporaryDirectory(prefix="polly-body-") as folder,
            FakeHTTP() as server,
        ):
            path = Path(folder) / "body.md"
            body = 'first\n"quoted"\n\u03bb\\n' + ("x" * 40000) + "\nlast"
            path.write_text(body)
            self.assertEqual(
                self.dispatch(
                    server,
                    [
                        "pr",
                        "comment",
                        "89",
                        "--repo",
                        "oz/omnigent",
                        "--body-file",
                        str(path),
                    ],
                ),
                "",
            )
            self.assertEqual(server.calls[-1][2], {"body": body})
            self.assertEqual(sum(r[0] == "POST" for r in server.calls), 1)

    def test_body_symlink_fifo_empty_or_oversize_refuse_before_post(self):
        with (
            tempfile.TemporaryDirectory(prefix="polly-body-") as folder,
            FakeHTTP() as server,
        ):
            folder = Path(folder)
            regular = folder / "regular"
            regular.write_text("synthetic")
            link = folder / "symlink"
            link.symlink_to(regular)
            fifo = folder / "fifo"
            os.mkfifo(fifo)
            empty = folder / "empty"
            empty.touch()
            large = folder / "large"
            large.write_bytes(b"x" * (m.COMMENT_LIMIT + 1))
            for path in (link, fifo, empty, large):
                with (
                    self.subTest(path=path.name),
                    self.assertRaises((m.Refusal, OSError)),
                ):
                    self.dispatch(
                        server,
                        [
                            "pr",
                            "comment",
                            "89",
                            "--repo",
                            "oz/omnigent",
                            "--body-file",
                            str(path),
                        ],
                    )
            self.assertFalse(any(r[0] == "POST" for r in server.calls))

    def test_no_association_promotion_full_metadata_and_diff(self):
        with FakeHTTP() as server:
            server.pull["author_association"] = "OWNER"
            self.assertEqual(
                self.dispatch(
                    server,
                    [
                        "api",
                        "repos/oz/omnigent/pulls/89",
                        "--jq",
                        '.author_association // "NONE"',
                    ],
                ),
                "NONE\n",
            )
            value = json.loads(
                self.dispatch(
                    server,
                    [
                        "pr",
                        "view",
                        "89",
                        "--repo",
                        "oz/omnigent",
                        "--json",
                        m.VIEW_FIELDS,
                    ],
                )
            )
            self.assertEqual(value["body"], server.pull["body"])
            self.assertEqual(value["changedFiles"], 3)
            self.assertIn(
                "full synthetic diff",
                self.dispatch(
                    server,
                    [
                        "api",
                        "repos/oz/omnigent/pulls/89",
                        "-H",
                        "Accept: application/vnd.github.v3.diff",
                    ],
                ),
            )

    def test_invalid_utf8_duplicate_json_and_body_bounds(self):
        for raw in (
            b"\xff",
            b'{"id":20001,"id":1,"login":"forgejo-actions"}',
            b"x" * (m.JSON_LIMIT + 1),
        ):
            with (
                self.subTest(size=len(raw)),
                FakeHTTP(overrides={("GET", "/api/v1/user"): (200, raw, {})}) as server,
            ):
                with self.assertRaises(m.Refusal):
                    self.dispatch(
                        server,
                        ["api", "repos/oz/omnigent/pulls/89", "--jq", ".head.sha"],
                    )
                self.assertEqual(len(server.calls), 1)

    def test_absolute_deadline_rejects_late_success(self):
        now = [0]
        with FakeHTTP() as server:
            transport = server.transport(clock=lambda: now[0])
            old = server.response

            def late(method, path):
                now[0] = m.TOTAL_SECONDS + 1
                return old(method, path)

            server.response = late
            with self.assertRaisesRegex(m.Refusal, "TRANSPORT_DEADLINE"):
                m.ForgeClient(BINDING, transport).dispatch(("head", "89", None))
            self.assertEqual(len(server.calls), 1)

    def test_post_lost_return_is_unknown_once(self):
        with FakeHTTP(
            overrides={
                ("POST", m.PREFIX + "/issues/comments/12/reactions"): (
                    200,
                    b"not json",
                    {},
                )
            }
        ) as server:
            with self.assertRaisesRegex(m.Refusal, "POST_OUTCOME_UNKNOWN_NO_RETRY"):
                self.dispatch(
                    server,
                    [
                        "api",
                        "repos/oz/omnigent/issues/comments/12/reactions",
                        "-f",
                        "content=eyes",
                        "--silent",
                    ],
                )
            self.assertEqual(sum(r[0] == "POST" for r in server.calls), 1)

    def test_global_alarm_bounds_dribbling_headers_disabled_red_enabled_green(self):
        elapsed = {}
        for label in ("socket_only", "absolute_alarm"):
            disable_alarm = (
                mock.patch.object(m.signal, "setitimer")
                if label == "socket_only"
                else nullcontext()
            )
            with (
                FakeHTTP(slow_headers=True) as server,
                disable_alarm,
                mock.patch.object(m, "FORGE_ACTIONS_BINDING", BINDING),
                mock.patch.object(m, "TOTAL_SECONDS", 0.06),
                mock.patch.object(
                    m.Transport,
                    "connection",
                    staticmethod(
                        lambda timeout: http.client.HTTPConnection(
                            "127.0.0.1", server.server.server_port, timeout=timeout
                        )
                    ),
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "GITHUB_SERVER_URL": m.ORIGIN,
                        "GITHUB_REPOSITORY": m.REPOSITORY,
                        "GH_TOKEN": "synthetic-actions-token",
                    },
                ),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                previous_handler = signal.getsignal(signal.SIGALRM)
                started = time.monotonic()
                self.assertEqual(
                    m.main(["api", "repos/oz/omnigent/pulls/89", "--jq", ".head.sha"]),
                    2,
                )
                elapsed[label] = time.monotonic() - started
                self.assertIn("TRANSPORT_DEADLINE", stderr.getvalue())
                self.assertEqual(signal.getsignal(signal.SIGALRM), previous_handler)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
                self.assertEqual(len(server.calls), 1)
        self.assertGreater(elapsed["socket_only"], 0.14)
        self.assertLess(elapsed["absolute_alarm"], 0.12)
        COMMANDS.append(
            {
                "causal_fixture": "dribbling_headers",
                "elapsed_seconds": elapsed,
                "baseline_change_only": "same_module_alarm_disabled_by_mock",
                "production_alarm_seconds": 25,
            }
        )

    def test_foreign_path_and_unsupported_jq_before_http(self):
        for argv in (
            ["api", "repos/other/omnigent/pulls/89", "--jq", ".head.sha"],
            ["api", "repos/oz/omnigent/pulls/89", "--jq", ".user.login"],
            ["pr", "comment", "89", "--repo", "oz/other", "--body-file", "unread"],
        ):
            with self.subTest(argv=argv), self.assertRaises(m.Refusal):
                m.parse_operation(argv)

    def test_unbound_source_bootstrap_creates_no_wrapper(self):
        result = self.shell(
            block(CANDIDATE.read_text(), "Select personal Forge platform client"),
            adapter=False,
            bootstrap_unbound=True,
        )
        self.assertEqual(result[0], 2)
        self.assertIn("ACTIONS_IDENTITY_UNBOUND", result[3])
        self.assertEqual(result[4], "")

    def test_unknown_actions_role_refuses_before_transport(self):
        for actor_id in (1, 2, -1, -3, 20001):
            binding = dict(BINDING, actor_id=actor_id)
            with (
                self.subTest(actor_id=actor_id),
                self.assertRaisesRegex(m.Refusal, "ACTIONS_ROLE_UNQUALIFIED"),
            ):
                m.ForgeClient(binding, None)
        with self.assertRaisesRegex(m.Refusal, "ACTIONS_ROLE_UNQUALIFIED"):
            m.ForgeClient(dict(BINDING, actor_login="agent-bot"), None)

    def test_source_binding_fixed_actions_tuple_and_repository(self):
        binding = m.binding_checked(m.FORGE_ACTIONS_BINDING)
        self.assertEqual(binding["actor_id"], -2)
        self.assertEqual(binding["actor_login"], "forgejo-actions")
        self.assertEqual(binding["repo_id"], 11)
        self.assertEqual(binding["repository"], "oz/omnigent")
        self.assertRegex(binding["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(m.ORIGIN, "https://forge.ozba.me")
        self.assertEqual(m.PREFIX, "/api/v1/repos/oz/omnigent")

    def test_forge_diff_refusal_stops_before_context_and_provider(self):
        override = {("GET", m.PREFIX + "/pulls/89.diff"): (500, "synthetic error", {})}
        with FakeHTTP(overrides=override) as server:
            result = self.shell(
                block(CANDIDATE.read_text(), "Collect PR context")
                + 'printf "provider-eligible\\n"\n',
                server,
            )
            self.assertEqual(result[0], 1)
            self.assertNotIn("provider-eligible", result[2])
            self.assertEqual(len(server.calls), 3)
            self.assertTrue(server.calls[-1][1].endswith(".diff"))

    def test_synthetic_bound_actual_bootstrap_missing_stock_gh(self):
        bootstrap = block(CANDIDATE.read_text(), "Select personal Forge platform client")
        head = (
            'export PATH="$(cat "$GITHUB_PATH"):$PATH"\n'
            'gh api "repos/$REPO/pulls/$PR_NUMBER" --jq .head.sha\n'
        )
        with FakeHTTP() as server:
            result = self.shell(bootstrap + head, server, adapter=False, bootstrap_binding=True)
            self.assertEqual(result[0], 0, result[3])
            self.assertEqual(result[2], HEAD + "\n")
            self.assertEqual(len(server.calls), 3)

    def test_workflow_github_condition_trusted_checkout_scan_and_eight_sites(self):
        workflow = CANDIDATE.read_text()
        start = workflow.index("      - name: Check out trusted Forge platform client")
        end = workflow.index("      - name: Validate /review command")
        bootstrap = workflow[start:end]
        self.assertEqual(bootstrap.count("if: github.server_url == 'https://forge.ozba.me'"), 2)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", bootstrap)
        self.assertIn("persist-credentials: false", bootstrap)
        self.assertIn("sparse-checkout: .github/scripts/polly", bootstrap)
        self.assertNotIn("pull_request.head", bootstrap)
        self.assertLess(bootstrap.index("--check-binding"), bootstrap.index("mktemp -d"))
        self.assertLess(bootstrap.index("--check-binding"), bootstrap.index('>> "$GITHUB_PATH"'))
        self.assertIn(
            "if: github.server_url == 'https://github.com' && steps.trigger.outputs.skip",
            workflow,
        )
        self.assertIn("GH_TOKEN: ${{ steps.app-token.outputs.token || github.token }}", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        trusted_start = workflow.index("      - name: Check out repo")
        trusted_end = workflow.index("      - name: Set up Python", trusted_start)
        self.assertIn(
            "ref: ${{ github.event.repository.default_branch }}",
            workflow[trusted_start:trusted_end],
        )
        self.assertIn("persist-credentials: false", workflow[trusted_start:trusted_end])
        sites = "\n".join(
            line
            for name in (
                "Validate /review command",
                "Skip if this commit already has a review",
                "Collect PR context",
                "Post review comment",
            )
            for line in block(workflow, name).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertEqual(sites.count("gh api "), 5)
        self.assertEqual(sites.count("gh pr "), 3)
        self.assertIn("timeout-minutes: 30", workflow)
        self.assertLess(
            workflow.index("      - name: Skip if this commit already has a review"),
            workflow.index("      - name: Check LLM credentials available"),
        )
        self.assertLess(
            workflow.index("      - name: Scan review output for secrets before posting"),
            workflow.index("      - name: Post review comment"),
        )
        self.assertIn(
            'grep -qF "$LLM_API_KEY" /tmp/polly_output.txt',
            block(workflow, "Scan review output for secrets before posting"),
        )
        self.assertIn(
            "if [ \"$GITHUB_SERVER_URL\" = 'https://forge.ozba.me' ]; then exit 1; fi",
            block(workflow, "Collect PR context"),
        )

    def test_final_comment_denial_is_terminal_and_never_retries(self):
        overrides = {("POST", m.PREFIX + "/issues/89/comments"): (403, {}, {})}
        with FakeHTTP(overrides=overrides) as server:
            result = self.shell(block(CANDIDATE.read_text(), "Post review comment"), server)
            self.assertEqual(result[0], 2)
            self.assertNotIn("Created new comment", result[2])
            self.assertIn("POST_OUTCOME_UNKNOWN_NO_RETRY", result[3])
            self.assertEqual(
                [row[:2] for row in server.calls],
                [
                    ("GET", "/api/v1/user"),
                    ("GET", m.PREFIX),
                    ("POST", m.PREFIX + "/issues/89/comments"),
                ],
            )
            self.assertIn("synthetic final review\n\u03bb", server.calls[-1][2]["body"])

    def test_authenticated_foreign_ids_and_login_refuse_before_own_repo(self):
        early = block(CANDIDATE.read_text(), "Skip if this commit already has a review")
        identities = [
            {"id": identifier, "login": "forgejo-actions"} for identifier in (1, 2, -1, -3, -4)
        ]
        identities.append({"id": -2, "login": "changed-login"})
        for identity in identities:
            with (
                self.subTest(identity=identity),
                FakeHTTP(overrides={("GET", "/api/v1/user"): (200, identity, {})}) as server,
            ):
                result = self.shell(early, server)
                self.assertEqual(result[0], 2)
                self.assertIn("ACTIONS_IDENTITY_DRIFT", result[3])
                self.assertEqual(result[1], "")
                self.assertEqual(len(server.calls), 1)
                self.assertEqual(server.calls[0][1], "/api/v1/user")

    def test_caps_and_binding_check_require_no_transport(self):
        self.assertEqual(m.TOTAL_SECONDS, 25)
        self.assertEqual(m.REQUEST_SECONDS, 5)
        self.assertEqual(m.JSON_LIMIT, 2 * 1024 * 1024)
        self.assertEqual(m.DIFF_LIMIT, 8 * 1024 * 1024)
        self.assertEqual(m.COMMENT_LIMIT, 65536)
        with (
            mock.patch.object(
                m, "Transport", side_effect=AssertionError("TRANSPORT_MUST_NOT_START")
            ),
            mock.patch.dict(
                os.environ,
                {"GITHUB_SERVER_URL": m.ORIGIN, "GITHUB_REPOSITORY": m.REPOSITORY},
                clear=True,
            ),
        ):
            self.assertEqual(m.main(["--check-binding"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
