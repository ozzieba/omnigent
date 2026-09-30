#!/usr/bin/env python3
"""Compute and apply the PR size label using trusted runner API settings."""

from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compute_label import size_label, total_changes
from trusted_ci_api import APIError, TrustedCIAPI, api_base


def apply_label(api: TrustedCIAPI, repository: str, pr_number: int) -> str:
    prefix = f"repos/{repository}"
    files = api.paginate(f"{prefix}/pulls/{pr_number}/files?per_page=100&page=1")
    label = size_label(total_changes(files))
    labels_path = f"{prefix}/labels/{urllib.parse.quote(label, safe='')}"
    try:
        api.get(labels_path)
    except APIError as error:
        if error.status != 404:
            raise
        api.request(
            "POST",
            f"{prefix}/labels",
            {
                "name": label,
                "color": "ededed",
                "description": f"Pull request size: {label.removeprefix('size/').upper()}",
            },
        )

    issue_labels = f"{prefix}/issues/{pr_number}/labels"
    api.request("POST", issue_labels, {"labels": [label]})
    current = api.get(issue_labels)
    for item in current:
        name = str(item.get("name") or "")
        if name.startswith("size/") and name != label:
            api.request("DELETE", f"{issue_labels}/{urllib.parse.quote(name, safe='')}")
    return label


def main() -> int:
    try:
        api = TrustedCIAPI(os.environ.get("GH_TOKEN", ""), api_base())
        repo = os.environ["REPO"]
        pr_number = int(os.environ["PR_NUMBER"])
        print(f"Computed: {apply_label(api, repo, pr_number)}")
        return 0
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
