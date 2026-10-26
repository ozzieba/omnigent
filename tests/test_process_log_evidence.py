from pathlib import Path
from unittest.mock import patch

import tests.conftest as root_conftest
from tests._process_log_evidence import preserve_process_log_summary


def test_failure_preserves_redacted_process_summary_before_cleanup(tmp_path: Path) -> None:
    data = tmp_path / "data"
    host = data / "logs" / "host"
    host.mkdir(parents=True)
    (host / "daemon.log").write_text(
        "INFO  10-26 21:15:21.551 host.connect _connect_and_serve | "
        "Connecting to https://example.invalid/?token=secret user message\n"
        "ERROR 10-26 21:15:22.000 host.connect _run | connection failed password=secret\n"
        "Traceback secret content\n"
    )
    progress = tmp_path / "progress" / "progress-gw1.log"
    with (
        patch.object(root_conftest, "_TEST_OMNIGENT_DATA_DIR", data),
        patch.object(root_conftest, "_PROGRESS_LOG_PATH", str(progress)),
        patch.object(root_conftest, "_FAILED_WORKER_TEST", True),
        patch(
            "omnigent.testing.process_reaper.reap_leaked_omnigent_processes", return_value=([], [])
        ),
    ):
        root_conftest.pytest_unconfigure(None)  # type: ignore[arg-type]
    text = (progress.parent / "progress-process-gw1.log").read_text()
    assert "host.connect _connect_and_serve categories=connection" in text
    assert "categories=connection,failure" in text
    assert "secret" not in text and "example.invalid" not in text and "user message" not in text
    assert not data.exists()


def test_success_does_not_read_or_copy_process_logs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    progress = tmp_path / "progress" / "progress-gw0.log"
    with (
        patch.object(root_conftest, "_TEST_OMNIGENT_DATA_DIR", data),
        patch.object(root_conftest, "_PROGRESS_LOG_PATH", str(progress)),
        patch.object(root_conftest, "_FAILED_WORKER_TEST", False),
        patch(
            "omnigent.testing.process_reaper.reap_leaked_omnigent_processes", return_value=([], [])
        ),
        patch("tests._process_log_evidence.preserve_process_log_summary") as preserve,
    ):
        root_conftest.pytest_unconfigure(None)  # type: ignore[arg-type]
    preserve.assert_not_called()
    assert not data.exists()


def test_preservation_ignores_symlinks_and_non_log_files(tmp_path: Path) -> None:
    host = tmp_path / "data" / "logs" / "host"
    host.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("secret")
    (host / "alias.log").symlink_to(secret)
    (host / "config.yaml").write_text("secret")
    output = tmp_path / "artifact.log"
    preserve_process_log_summary(tmp_path / "data", output)
    assert not output.exists()
