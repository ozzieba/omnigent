from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apply_label import apply_label
from trusted_ci_api import APIError


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def paginate(self, path: str):
        self.calls.append(("GET-PAGES", path, None))
        return [{"filename": "docs/change.md", "additions": 5, "deletions": 1}]

    def get(self, path: str):
        self.calls.append(("GET", path, None))
        if path.endswith("/labels/size%2FXS"):
            raise APIError(404)
        if path.endswith("/issues/12/labels"):
            return [{"name": "size/M"}, {"name": "help wanted"}]
        raise AssertionError(f"unexpected GET {path}")

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        return None, None


class ApplyLabelTests(unittest.TestCase):
    def test_creates_desired_label_applies_it_and_removes_only_stale_size_labels(self):
        api = FakeAPI()
        label = apply_label(api, "org/repo", 12)
        self.assertEqual(label, "size/XS")
        self.assertIn(
            (
                "POST",
                "repos/org/repo/labels",
                {
                    "name": "size/XS",
                    "color": "ededed",
                    "description": "Pull request size: XS",
                },
            ),
            api.calls,
        )
        self.assertIn(
            ("POST", "repos/org/repo/issues/12/labels", {"labels": ["size/XS"]}),
            api.calls,
        )
        self.assertIn(
            ("DELETE", "repos/org/repo/issues/12/labels/size%2FM", None),
            api.calls,
        )
        self.assertFalse(any("help%20wanted" in call[1] for call in api.calls))


if __name__ == "__main__":
    unittest.main()
