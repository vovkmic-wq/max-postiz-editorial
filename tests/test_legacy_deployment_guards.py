"""Прежние команды не должны менять настройки или возвращать Hub после миграции."""
import importlib.util
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DIRECTORY = Path(__file__).resolve().parents[1] / "deployment/linux"
SPEC = importlib.util.spec_from_file_location("legacy_guard", DIRECTORY / "legacy_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


@pytest.mark.parametrize("detected", ["marker", "unit"])
def test_known_migration_blocks_legacy_before_commands(tmp_path, detected):
    paths = [tmp_path / "max-postiz-editorial.service"]
    marker = tmp_path / "data/hourly-articles/postgresql-authoritative.local.json"
    selected = marker if detected == "marker" else paths[0]
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text("proof", encoding="utf-8")

    def unexpected(*_, **__):
        pytest.fail("После обнаруженной миграции внешняя команда не нужна")

    with pytest.raises(SystemExit, match="LEGACY_COMMAND_RETIRED.*VM_EDITORIAL"):
        guard.refuse_legacy(tmp_path, runner=unexpected, unit_paths=paths)
    assert selected.read_text(encoding="utf-8") == "proof"


@pytest.mark.parametrize("state,code,allowed", [
    ("not-found", 0, True), ("loaded", 0, False), ("masked", 0, False),
    ("", 1, False), ("not-found", 1, False),
])
def test_systemd_detection_is_read_only_and_fails_closed(tmp_path, state, code, allowed):
    seen = []

    def runner(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(stdout=state, returncode=code)

    if allowed:
        guard.refuse_legacy(tmp_path, runner=runner, unit_paths=[])
    else:
        with pytest.raises(SystemExit, match="VM_EDITORIAL"):
            guard.refuse_legacy(tmp_path, runner=runner, unit_paths=[])
    assert seen[0][0] == ["systemctl", "show", "--property=LoadState", "--value",
                          "max-postiz-editorial.service"]
    assert seen[0][1]["timeout"] == 5
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 5)])
def test_unavailable_systemd_never_authorizes_legacy_changes(tmp_path, error):
    def unavailable(*_, **__):
        raise error

    with pytest.raises(SystemExit, match="LEGACY_STATE_UNAVAILABLE.*VM_EDITORIAL"):
        guard.refuse_legacy(tmp_path, runner=unavailable, unit_paths=[])


@pytest.mark.parametrize("script", ["configure-runtime.py", "verify-runtime.py"])
def test_python_legacy_scripts_stop_before_configuration_or_database(monkeypatch, script):
    checked = []

    def refuse(root):
        checked.append(root)
        raise SystemExit("LEGACY_COMMAND_RETIRED")

    monkeypatch.setitem(sys.modules, "legacy_guard", guard)
    monkeypatch.setattr(guard, "refuse_legacy", refuse)
    monkeypatch.setattr("max_postiz_agent.cli.load_env", lambda *_: pytest.fail("Не читать credentials"))
    monkeypatch.setattr("secrets.token_urlsafe", lambda *_: pytest.fail("Не менять credentials"))
    with pytest.raises(SystemExit, match="LEGACY_COMMAND_RETIRED"):
        runpy.run_path(str(DIRECTORY / script))
    assert checked == [Path("/opt/max-postiz")]


def bash_binary():
    # WSL launcher без установленной Linux не подходит для локальных фикстур.
    if os.name == "nt":
        path = Path("C:/Program Files/Git/bin/bash.exe")
        return str(path) if path.is_file() else None
    return shutil.which("bash")


@pytest.mark.parametrize("script", ["network-and-tools.sh", "install-owner-display.sh"])
def test_shell_legacy_scripts_check_guard_before_any_mutation(tmp_path, script):
    bash = bash_binary()
    if not bash:
        pytest.skip("Для shell-фикстуры нужен bash")
    events = tmp_path / "commands.txt"
    shell = '''
python3() { printf '%s\\n' "$*" >> "$EVENTS"; return 73; }
export -f python3
bash "$SCRIPT"
'''
    result = subprocess.run([bash, "-c", shell], capture_output=True, text=True, timeout=10,
        env={**os.environ, "EVENTS": events.as_posix(), "SCRIPT": (DIRECTORY / script).as_posix()})
    assert result.returncode == 73
    assert events.read_text(encoding="utf-8").strip().endswith("/legacy_guard.py")
    assert len(events.read_text(encoding="utf-8").splitlines()) == 1


def test_browser_installer_never_starts_hub_or_an_inactive_editorial_worker(tmp_path):
    bash = bash_binary()
    if not bash:
        pytest.skip("Для shell-фикстуры нужен bash")
    events = tmp_path / "commands.txt"
    shell = '''
curl() { :; }
apt-get() { :; }
google-chrome() { :; }
test() { return 0; }
python3() { cat >/dev/null; }
systemctl() { printf '%s\\n' "$*" >> "$EVENTS"; }
export -f curl apt-get google-chrome test python3 systemctl
bash "$SCRIPT"
'''
    result = subprocess.run([bash, "-c", shell], capture_output=True, text=True, timeout=10,
        env={**os.environ, "EVENTS": events.as_posix(),
             "SCRIPT": (DIRECTORY / "install-browser.sh").as_posix()})
    assert result.returncode == 0, result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "try-restart max-postiz-editorial.service"]
