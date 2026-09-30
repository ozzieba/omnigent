from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("install.sh")
PINNED_REQUIREMENT = (
    "uv==0.12.9 "
    "--hash=sha256:5badfd805fd88bf99b4b4f044f6e8f762f1892cab27477f4427bb473e93dd049"
)


class PinnedUvInstallerTests(unittest.TestCase):
    def test_installs_into_task_private_target_and_exposes_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner_temp = root / "runner-temp"
            fake_bin = root / "bin"
            runner_temp.mkdir()
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$1 $2\" == '-m pip' ]]; then\n"
                "  shift 2\n"
                "  while (($#)); do\n"
                "    case \"$1\" in\n"
                "      --target) target=$2; shift 2 ;;\n"
                "      -r) requirements=$2; shift 2 ;;\n"
                "      *) shift ;;\n"
                "    esac\n"
                "  done\n"
                "  [[ \"$(cat \"$requirements\")\" == \""
                + PINNED_REQUIREMENT
                + "\" ]]\n"
                "  mkdir -p \"$target/uv\"\n"
                "  [[ \"$target\" == *'/runner-temp/uv-tools/site' ]]\n"
                "elif [[ \"$1 $2\" == '-m uv' && \"${PYTHONPATH:-}\" == "
                "*'/uv-tools/site'* ]]; then\n"
                "  echo 'uv 0.12.9'\n"
                "else\n"
                "  exit 41\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            path_file = root / "github-path"
            env = dict(os.environ)
            env.update(
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_PATH": str(path_file),
                    "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("uv 0.12.9", result.stdout)
            self.assertEqual(path_file.read_text().strip(), str(runner_temp / "uv-tools"))
            wrapper = runner_temp / "uv-tools" / "uv"
            self.assertTrue(os.access(wrapper, os.X_OK))
            checked = subprocess.run(
                [str(wrapper), "--version"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(checked.stdout.strip(), "uv 0.12.9")


if __name__ == "__main__":
    unittest.main()
