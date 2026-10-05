"""Repository guards on workflow jobs that deploy to external infrastructure.

The workflows in this repository also run wherever the repository is mirrored
(GitHub forks, the private Forgejo working remote). A job that creates, deploys
or deletes Databricks Apps must only run in the upstream repository: anywhere
else it either deploys from an unreviewed mirror with whatever secrets that
mirror holds, or burns a runner just to fail at auth on every push to ``main``.
The protected-Environment gate in ``ui-preview.yml`` does not help here: it is
GitHub-only and a no-op on Forgejo.

The convention (``doc-sync.yml``, ``release.yml``, ...) is a job-level
``if: github.repository == 'omnigent-ai/omnigent'``. This test enforces it for
every job that drives ``databricks apps``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
UPSTREAM_GUARD = "github.repository == 'omnigent-ai/omnigent'"
# A job matches when any of these appear anywhere in its definition (steps,
# env, run scripts). Deliberately the Apps CLI only: workflows that merely
# call the Databricks model gateway (issue-prioritization-v2.yml) are not
# deploys and are gated by their callers.
DEPLOY_MARKERS = ("databricks apps ",)


def _databricks_apps_jobs() -> list[tuple[str, str, dict[str, object]]]:
    found: list[tuple[str, str, dict[str, object]]] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        jobs = (doc or {}).get("jobs") or {}
        for job_name, job in jobs.items():
            blob = json.dumps(job)
            if any(marker in blob for marker in DEPLOY_MARKERS):
                found.append((path.name, job_name, job))
    return found


_JOBS = _databricks_apps_jobs()


def test_discovery_finds_the_ui_preview_deploy_jobs() -> None:
    """Guard against the discovery itself going quiet.

    If the markers drift (CLI renamed, secret renamed) this test fails loudly
    instead of the parametrized check below silently collecting nothing.
    """
    names = {(workflow, job) for workflow, job, _ in _JOBS}
    assert {("ui-preview.yml", "deploy"), ("ui-preview.yml", "cleanup")} <= names, names


@pytest.mark.parametrize(
    ("workflow", "job_name", "job"),
    [pytest.param(w, j, body, id=f"{w}::{j}") for w, j, body in _JOBS],
)
def test_databricks_apps_jobs_only_run_in_the_upstream_repo(
    workflow: str, job_name: str, job: dict[str, object]
) -> None:
    condition = str(job.get("if") or "")
    assert UPSTREAM_GUARD in condition, (
        f"{workflow} job '{job_name}' drives Databricks Apps but is not gated on "
        f"`{UPSTREAM_GUARD}`; on a mirror or fork it would deploy from unreviewed "
        f"code (or fail at auth on every run). Current `if`: {condition!r}"
    )
