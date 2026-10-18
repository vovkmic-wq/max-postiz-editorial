"""Exercise the real Bash guard with mocked host commands; never touch a NIC."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deployment/proxmox/guard-eno1-tso.sh"
SERVICE = ROOT / "deployment/proxmox/max-postiz-eno1-tso.service"


def bash_path():
    if os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        if candidate.is_file():
            return str(candidate)
        pytest.skip("Git Bash is required for isolated Bash guard tests on Windows")
    return shutil.which("bash") or pytest.skip("bash is unavailable")


@pytest.fixture
def host(tmp_path):
    (tmp_path / "tso").write_text("on\n", encoding="utf-8")
    environment = tmp_path / "mock-host.sh"
    environment.write_text(r'''hostname() { printf '%s\n' "${FAKE_HOST:-pve}"; }
uname() { printf '%s\n' "${FAKE_KERNEL:-6.8.12-43-pve}"; }
id() { printf '%s\n' "${FAKE_UID:-0}"; }
pveversion() { printf '%s\n' 'pve-manager/8.4'; }
cat() {
  case "$1" in
    /sys/class/net/eno1/address) printf '%s\n' "${FAKE_MAC:-f4:b5:20:4d:ed:08}" ;;
    /sys/class/net/eno1/device/vendor) printf '%s\n' "${FAKE_VENDOR:-0x8086}" ;;
    /sys/class/net/eno1/device/device) printf '%s\n' "${FAKE_DEVICE:-0x1a1d}" ;;
    *) command cat "$@" ;;
  esac
}
readlink() {
  case "${@: -1}" in
    /sys/class/net/eno1/device) printf '%s\n' "${FAKE_PCI:-/sys/devices/pci0000:00/0000:00:1f.6}" ;;
    /sys/class/net/eno1/device/driver) printf '%s\n' "${FAKE_DRIVER_PATH:-/sys/bus/pci/drivers/e1000e}" ;;
    *) return 1 ;;
  esac
}
ethtool() {
  printf '%s\n' "$*" >> "$TEST_DIRECTORY/calls"
  case "$1" in
    -i) printf 'driver: %s\nversion: 6.8.12-43-pve\nbus-info: %s\n' "${FAKE_DRIVER:-e1000e}" "${FAKE_BUS:-0000:00:1f.6}" ;;
    -k)
      [[ "${FAKE_QUERY_FAIL:-0}" == 0 ]] || return 1
      state=$(command cat "$TEST_DIRECTORY/tso")
      gso=on
      if [[ "${FAKE_PROTECTED_CHANGE:-0}" == 1 && "$state" == off ]]; then gso=off; fi
      printf 'Features for eno1:\ntcp-segmentation-offload: %s\ngeneric-segmentation-offload: %s\ngeneric-receive-offload: on\nrx-checksumming: on\ntx-checksumming: on\n' "$state" "$gso"
      ;;
    -K)
      [[ "$*" == '-K eno1 tso off' ]] || return 99
      [[ "${FAKE_APPLY_FAIL:-0}" == 0 ]] || return 5
      if [[ "${FAKE_IGNORE_CHANGE:-0}" == 0 ]]; then printf 'off\n' > "$TEST_DIRECTORY/tso"; fi
      ;;
    *) return 99 ;;
  esac
}
''', encoding="utf-8", newline="\n")

    def run(*args, **overrides):
        env = {**os.environ, "BASH_ENV": environment.as_posix(),
               "TEST_DIRECTORY": tmp_path.as_posix(), **overrides}
        result = subprocess.run([bash_path(), SCRIPT.as_posix(), *args], env=env,
                                text=True, capture_output=True, timeout=10, check=False)
        calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
        return result, calls
    return run, tmp_path


@pytest.mark.parametrize("arguments", [(), ("--check",)])
def test_default_and_explicit_check_never_change_a_feature(host, arguments):
    run, directory = host
    result, calls = run(*arguments, FAKE_UID="1000")
    assert result.returncode == 0 and json.loads(result.stdout)["mode"] == "check"
    assert calls == ["-i eno1", "-k eno1"]
    assert (directory / "tso").read_text().strip() == "on"


def test_apply_disables_only_tso_and_rechecks_features(host):
    run, directory = host
    result, calls = run("--apply")
    assert result.returncode == 0 and json.loads(result.stdout)["changed"] is True
    assert calls == ["-i eno1", "-k eno1", "-K eno1 tso off", "-k eno1"]
    assert (directory / "tso").read_text().strip() == "off"


def test_already_disabled_tso_is_idempotent(host):
    run, directory = host
    (directory / "tso").write_text("off\n")
    result, calls = run("--apply")
    assert result.returncode == 0 and json.loads(result.stdout)["changed"] is False
    assert calls == ["-i eno1", "-k eno1"]


@pytest.mark.parametrize("override", [
    {"FAKE_HOST": "other"}, {"FAKE_KERNEL": "6.8.12-generic"},
    {"FAKE_MAC": "f4:b5:20:4d:ed:09"}, {"FAKE_PCI": "/sys/devices/pci0000:00/0000:00:1e.6"},
    {"FAKE_VENDOR": "0x1234"}, {"FAKE_DEVICE": "0x1234"},
    {"FAKE_DRIVER_PATH": "/sys/bus/pci/drivers/igb"}, {"FAKE_DRIVER": "igb"},
    {"FAKE_BUS": "0000:00:1e.6"}, {"FAKE_UID": "1000"}, {"FAKE_QUERY_FAIL": "1"},
])
def test_wrong_host_or_unreadable_state_blocks_apply_before_mutation(host, override):
    run, directory = host
    result, calls = run("--apply", **override)
    assert result.returncode != 0
    assert not any(call.startswith("-K") for call in calls)
    assert (directory / "tso").read_text().strip() == "on"


@pytest.mark.parametrize(("override", "error"), [
    ({"FAKE_APPLY_FAIL": "1"}, "TSO_CHANGE_FAILED"),
    ({"FAKE_IGNORE_CHANGE": "1"}, "TSO_VERIFY_FAILED"),
    ({"FAKE_PROTECTED_CHANGE": "1"}, "PROTECTED_FEATURE_CHANGED"),
])
def test_failed_apply_or_verification_never_reports_success_or_tries_a_reset(host, override, error):
    run, _ = host
    result, calls = run("--apply", **override)
    assert result.returncode != 0 and error in result.stderr
    assert not result.stdout
    assert [call for call in calls if call.startswith("-K")] == ["-K eno1 tso off"]


def test_unknown_option_is_rejected_before_inspection(host):
    run, _ = host
    result, calls = run("--reset")
    assert result.returncode == 64 and not calls


def test_service_only_applies_the_guard_and_never_reenables_tso_implicitly():
    unit = SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    assert "After=network.target sys-subsystem-net-devices-eno1.device" in unit
    assert "Before=pve-guests.service" in unit and "WantedBy=multi-user.target" in unit
    assert "JobTimeoutSec=30" in unit and "TimeoutStartSec=20" in unit
    assert "ExecStart=/usr/local/sbin/max-postiz-eno1-tso --apply" in unit
    assert "ExecStop=" not in unit and "Restart=" not in unit
