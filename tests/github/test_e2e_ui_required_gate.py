"""Run the actual gate and workflow shell block with offline API/judge fixtures."""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ".github/scripts/e2e-ui-required/check.sh"
WORKFLOW = ".github/workflows/e2e-ui-required.yml"


def workflow_block(source):
    lines = source.splitlines()
    start = lines.index("      - name: Require e2e_ui coverage or effective waiver")
    run = next(i for i in range(start, len(lines)) if lines[i] == "        run: |")
    block = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith("          "):
            break
        block.append(line)
    return textwrap.dedent("\n".join(block))


FAKE_GH = """#!/usr/bin/python3
import json, os, pathlib, subprocess, sys
p=pathlib.Path(os.environ['GATE_FIXTURE_DIR'])
c=json.loads((p/'case.json').read_text()); a=sys.argv[1:]
with (p/'gh.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')
if a[0]=='api' and a[1].endswith('/files'): data=c['files']
elif a[0]=='api' and a[1].endswith('/reviews'): data=c['reviews']
elif a[0]=='api' and a[1]=='repos/oz/omnigent/pulls/86':
 data={'labels':[{'name':'skip-e2e-ui-test'}] if c['label'] else []}
elif a[:3]==['pr','view','86']: data={'title':'Synthetic fixture', 'author':{'login':c['author']}}
else: raise SystemExit('Unexpected fixture gh route')
if '--jq' in a:
 r=subprocess.run(['/usr/bin/jq','-r',a[a.index('--jq')+1]],input=json.dumps(data).encode(),capture_output=True)
 sys.stdout.buffer.write(r.stdout); sys.stderr.buffer.write(r.stderr)
 raise SystemExit(r.returncode)
print(json.dumps(data))
"""

FAKE_CURL = """#!/usr/bin/python3
import json, os, pathlib, sys
p=pathlib.Path(os.environ['GATE_FIXTURE_DIR'])
c=json.loads((p/'case.json').read_text()); a=sys.argv[1:]
assert a[a.index('-X')+1]=='POST'
assert c['expected_base'].rstrip('/')+'/chat/completions' in a
assert 'Authorization: Bearer '+c['expected_key'] in a
body=json.loads(a[a.index('-d')+1])
assert body['max_tokens']==200 and body['temperature']==0
with (p/'curl.jsonl').open('a') as f:
 f.write(json.dumps({'model':body['model'],'messages':len(body['messages'])})+'\\n')
if c['curl_error']: raise SystemExit(22)
verdict=json.dumps({'needs_test':c['needs_test'],'reason':'Synthetic verdict'})
print(json.dumps({'choices':[{'message':{'content':verdict}}]}))
"""


def run_fixture(
    name,
    *,
    web=False,
    model=None,
    workflow=True,
    needs_test=False,
    label=False,
    author="contributor",
    reviews=(),
    curl_error=False,
    judge_base=None,
    judge_key=None,
    legacy_credentials=True,
    source_root=ROOT,
):
    parent = os.environ.get("E2E_GATE_FIXTURE_ROOT")
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=name + "-", dir=parent))
    binary = directory / "bin"
    binary.mkdir(mode=0o700)
    for command, content in (("gh", FAKE_GH), ("curl", FAKE_CURL)):
        target = binary / command
        target.write_text(content)
        target.chmod(0o700)
    script = directory / SCRIPT
    script.parent.mkdir(parents=True, mode=0o700)
    script.write_bytes((source_root / SCRIPT).read_bytes())
    files = [
        {
            "status": "modified",
            "filename": "web/Chat.tsx" if web else "omnigent/cli.py",
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }
    ]
    if name.startswith("docs"):
        files[0]["filename"] = "docs/HOST_ADMISSION.md"
    (directory / "case.json").write_text(
        json.dumps(
            {
                "files": files,
                "needs_test": needs_test,
                "label": label,
                "author": author,
                "reviews": list(reviews),
                "curl_error": curl_error,
                "expected_base": judge_base or "https://hermetic.invalid",
                "expected_key": judge_key or "synthetic",
            }
        )
    )
    env = {
        "PATH": str(binary) + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GATE_FIXTURE_DIR": str(directory),
        "REPO": "oz/omnigent",
        "PR": "86",
        "GH_TOKEN": "synthetic-not-a-credential",
        "MAINTAINERS": "oz",
    }
    if legacy_credentials:
        env["OPENAI_BASE_URL"] = "https://hermetic.invalid"
        env["OPENAI_API_KEY"] = "synthetic"
    if judge_base is not None:
        env["E2E_UI_JUDGE_BASE_URL"] = judge_base
    if judge_key is not None:
        env["E2E_UI_JUDGE_API_KEY"] = judge_key
    if model is not None:
        env["E2E_UI_JUDGE_MODEL"] = model
    if workflow:
        block = workflow_block((source_root / WORKFLOW).read_text())
        command = ["/bin/bash", "-e", "-o", "pipefail", "-c", block]
    else:
        command = ["/bin/bash", SCRIPT]
    cp = subprocess.run(command, cwd=directory, env=env, capture_output=True, timeout=8)
    (directory / "stdout.txt").write_bytes(cp.stdout)
    (directory / "stderr.txt").write_bytes(cp.stderr)

    def entries(name):
        file = directory / name
        return (
            [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []
        )

    return cp, entries("gh.jsonl"), entries("curl.jsonl"), directory


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path("/usr/bin/jq").is_file() or not shutil.which("bash"):
            raise RuntimeError(
                "These offline gate tests require the maintained bash and jq prerequisites"
            )

    def no_ui(self, name, model, workflow):
        cp, gh, curl, _ = run_fixture(name, model=model, workflow=workflow)
        self.assertEqual(cp.returncode, 0, cp.stderr.decode())
        self.assertIn(b"PR touches no web/**", cp.stdout)
        self.assertEqual(len(gh), 1)
        self.assertEqual(curl, [])

    def web_unconfigured(self, model, workflow):
        cp, gh, curl, _ = run_fixture("web-unconfigured", web=True, model=model, workflow=workflow)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn(b"E2E_UI_JUDGE_MODEL", cp.stderr)
        self.assertEqual(len(gh), 1)  # No second files request, title, diff or judge construction.
        self.assertEqual(curl, [])

    def configured(self, name, *, success, **kwargs):
        cp, _, curl, _ = run_fixture(name, web=True, model="fixture-judge-model", **kwargs)
        self.assertEqual(cp.returncode, 0 if success else 1, cp.stderr.decode())
        self.assertEqual(curl, [{"model": "fixture-judge-model", "messages": 2}])
        return cp

    def test_backend_workflow_unset(self):
        self.no_ui("backend-unset", None, True)

    def test_backend_workflow_empty(self):
        self.no_ui("backend-empty", "", True)

    def test_docs_workflow_unset(self):
        self.no_ui("docs-unset", None, True)

    def test_docs_workflow_empty(self):
        self.no_ui("docs-empty", "", True)

    def test_backend_script_unset(self):
        self.no_ui("backend-script-unset", None, False)

    def test_docs_script_empty(self):
        self.no_ui("docs-script-empty", "", False)

    def test_web_workflow_unset(self):
        self.web_unconfigured(None, True)

    def test_web_workflow_empty(self):
        self.web_unconfigured("", True)

    def test_web_script_unset(self):
        self.web_unconfigured(None, False)

    def test_web_script_empty(self):
        self.web_unconfigured("", False)

    def test_configured_judge_pass(self):
        cp = self.configured("judge-pass", success=True)
        self.assertIn(b"judge -> no test required", cp.stdout)

    def test_configured_judge_needs_test_blocks(self):
        cp = self.configured("judge-block", success=False, needs_test=True)
        self.assertIn(b"without a tests/e2e_ui/** test", cp.stdout)

    def test_unapproved_waiver_blocks(self):
        cp = self.configured("unapproved-waiver", success=False, needs_test=True, label=True)
        self.assertIn(b"not effective", cp.stdout)

    def test_later_changes_requested_blocks_waiver(self):
        reviews = [
            {"user": {"login": "oz"}, "state": state, "submitted_at": date}
            for state, date in [
                ("APPROVED", "2026-01-01T00:00:00Z"),
                ("CHANGES_REQUESTED", "2026-01-02T00:00:00Z"),
            ]
        ]
        self.configured(
            "changes-requested", success=False, needs_test=True, label=True, reviews=reviews
        )

    def test_approved_waiver_survives_later_comment(self):
        reviews = [
            {"user": {"login": "oz"}, "state": state, "submitted_at": date}
            for state, date in [
                ("APPROVED", "2026-01-01T00:00:00Z"),
                ("COMMENTED", "2026-01-02T00:00:00Z"),
            ]
        ]
        self.configured(
            "approved-waiver", success=True, needs_test=True, label=True, reviews=reviews
        )

    def test_maintainer_author_waiver(self):
        self.configured("author-waiver", success=True, needs_test=True, label=True, author="oz")

    def test_gateway_error_remains_closed(self):
        cp = self.configured("gateway-failure", success=False, curl_error=True)
        self.assertIn(b"gateway error, exit 22", cp.stdout)

    def test_dedicated_pair_overrides_shared_credentials(self):
        for workflow in (False, True):
            with self.subTest(workflow=workflow):
                cp = self.configured(
                    "dedicated-pair",
                    success=True,
                    workflow=workflow,
                    judge_base="https://judge.hermetic.invalid/v1/",
                    judge_key="synthetic-dedicated",
                )
                self.assertNotIn(b"synthetic-dedicated", cp.stdout + cp.stderr)

    def test_dedicated_pair_without_shared_credentials(self):
        self.configured(
            "dedicated-only",
            success=True,
            judge_base="https://judge.hermetic.invalid/v1",
            judge_key="synthetic-dedicated",
            legacy_credentials=False,
        )

    def test_partial_pair_refuses_before_diff_or_http(self):
        for judge_base, judge_key in (
            ("https://judge.hermetic.invalid/v1", None),
            ("https://judge.hermetic.invalid/v1", ""),
            (None, "synthetic-dedicated"),
            ("", "synthetic-dedicated"),
        ):
            for workflow in (False, True):
                with self.subTest(judge_base=judge_base, judge_key=judge_key, workflow=workflow):
                    cp, gh, curl, _ = run_fixture(
                        "partial-pair",
                        web=True,
                        model="fixture-judge-model",
                        judge_base=judge_base,
                        judge_key=judge_key,
                        workflow=workflow,
                    )
                    self.assertNotEqual(cp.returncode, 0)
                    self.assertIn(b"Set both OMNIGENT_UI_JUDGE", cp.stderr)
                    self.assertEqual(len(gh), 1)
                    self.assertEqual(curl, [])
                    self.assertNotIn(b"synthetic-dedicated", cp.stdout + cp.stderr)

    def test_no_ui_partial_pair_still_skips(self):
        for judge_base, judge_key in (
            ("https://judge.hermetic.invalid/v1", None),
            (None, "synthetic-dedicated"),
        ):
            for workflow in (False, True):
                with self.subTest(judge_base=judge_base, judge_key=judge_key, workflow=workflow):
                    cp, gh, curl, _ = run_fixture(
                        "backend-partial-pair",
                        judge_base=judge_base,
                        judge_key=judge_key,
                        workflow=workflow,
                        legacy_credentials=False,
                    )
                    self.assertEqual(cp.returncode, 0, cp.stderr.decode())
                    self.assertIn(b"PR touches no web/**", cp.stdout)
                    self.assertEqual(len(gh), 1)
                    self.assertEqual(curl, [])

    def test_empty_pair_uses_shared_credentials(self):
        self.configured("empty-pair", success=True, judge_base="", judge_key="")

    def test_web_without_any_credentials_refuses_before_http(self):
        cp, gh, curl, _ = run_fixture(
            "web-no-credentials", web=True, model="fixture-judge-model", legacy_credentials=False
        )
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn(b"GATEWAY_BASE_URL", cp.stderr)
        self.assertEqual(len(gh), 1)
        self.assertEqual(curl, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
