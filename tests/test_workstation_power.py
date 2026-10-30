"""Exercise the actual PowerShell script with an isolated native runner mock."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_power_script_mock_controls_and_restore(tmp_path):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required for native script contract tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File",
        str(root / "tests/workstation-power-mocks.ps1"), "-ProjectRoot", str(tmp_path),
        "-ScriptPath", str(root / "scripts/configure-workstation-power.ps1")],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr[-3000:]
    summary = json.loads(result.stdout)
    assert summary == {"passed": 10, "real_power_mutations": 0, "native_runner": "mock"}
