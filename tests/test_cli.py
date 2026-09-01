"""CLI process-boundary regressions."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_python_module_propagates_cli_validation_exit_code() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent", "investigate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Provide a question, or use --playbook." in result.stderr
