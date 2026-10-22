"""Forge latest noncomment review states must match the unchanged Gate jq policy."""

import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "gate_wire", ROOT / "tests/github/test_ui_gate_forge_platform.py"
)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def tearDownModule():
    if os.environ.get("CASE_REVIEW_RECEIPT"):
        Path(os.environ["CASE_REVIEW_RECEIPT"]).write_text(json.dumps(h.COMMANDS, indent=2) + "\n")


def github_reviews(rows):
    """Only native enum spelling/dismissal translation; query is unchanged."""
    result = []
    for row in rows:
        value = dict(row)
        value["state"] = (
            "DISMISSED"
            if row["dismissed"]
            else {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
                row["state"], row["state"]
            )
        )
        result.append(value)
    return result


def jq_approvers(rows):
    # jq is an existing maintained Gate prerequisite. Only synthetic input.
    process = subprocess.run(
        ["jq", "-r", h.m.REVIEW_QUERY],
        input=json.dumps(github_reviews(rows)).encode(),
        capture_output=True,
        timeout=2,
        check=True,
    )
    return process.stdout.decode()


class Cases(unittest.TestCase):
    def approvers(self, rows):
        with h.Wire(reviews=rows) as server:
            result = h.Cases().operation(
                server,
                "api",
                "repos/oz/omnigent/pulls/89/reviews",
                "--paginate",
                "--jq",
                h.m.REVIEW_QUERY,
            )
            self.assertEqual(len(server.calls), 5)
            self.assertTrue(all(method == "GET" for method, _ in server.calls))
            return result

    def test_pending_and_requested_review_suppress_earlier_approval(self):
        for state in ("PENDING", "REQUEST_REVIEW"):
            with self.subTest(state=state):
                rows = [h.review(), h.review(2, state)]
                self.assertEqual(jq_approvers(rows), "")
                self.assertEqual(self.approvers(rows), jq_approvers(rows))

    def test_comment_changes_requested_and_dismissed_match_original_query(self):
        for state, dismissed, expected in (
            ("COMMENT", False, "oz\n"),
            ("REQUEST_CHANGES", False, ""),
            ("APPROVED", True, ""),
            ("COMMENT", True, ""),
        ):
            with self.subTest(state=state, dismissed=dismissed):
                rows = [h.review(), h.review(2, state, dismissed=dismissed)]
                self.assertEqual(jq_approvers(rows), expected)
                self.assertEqual(self.approvers(rows), expected)

    def test_newer_approval_and_other_reviewer_remain_independent(self):
        rows = [
            h.review(),
            h.review(2, "PENDING"),
            h.review(3),
            h.review(4, "REQUEST_REVIEW", login="other"),
        ]
        self.assertEqual(jq_approvers(rows), "oz\n")
        self.assertEqual(self.approvers(rows), jq_approvers(rows))

    def test_actual_gate_shell_pending_or_requested_review_cannot_waive(self):
        for state in ("PENDING", "REQUEST_REVIEW"):
            with (
                self.subTest(state=state),
                h.Wire(reviews=[h.review(), h.review(2, state)]) as server,
            ):
                server.pull["labels"] = [{"name": "skip-e2e-ui-test"}]
                result = h.Cases().shell(server, needs_test=True)
                self.assertEqual(result[0], 1)
                self.assertIn("not effective", result[1])
                self.assertEqual(result[3], 1)  # synthetic curl only


if __name__ == "__main__":
    unittest.main()
