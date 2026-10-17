import errno
import importlib.util
import json
import os
import socket
import struct
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-network.py"
SPEC = importlib.util.spec_from_file_location("network_diagnostics", SCRIPT)
network = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network)


def observations(reachable):
    return [{**target, "reachable": target["service"] in reachable, "error": None,
             "elapsed_ms": 0} for target in network.TARGETS]


def test_gpu_answer_does_not_get_diagnosed_as_broken_vpn_or_powered_off_host():
    summary = network.classify(observations({"gpu_ssh", "gpu_model", "gateway_http"}))
    assert summary["observation"] == "proxmox_and_guests_unreachable_gpu_reachable"
    assert summary["vpn_fault"] == summary["power_state"] == "unconfirmed"
    assert not summary["core_reachable"]


def test_no_responses_leave_failure_scope_unconfirmed():
    summary = network.classify(observations(set()))
    assert summary["observation"] == "tested_endpoints_unreachable"
    assert summary["vpn_fault"] == summary["power_state"] == "unconfirmed"


def test_gateway_http_is_diagnostic_only_not_an_application_readiness_requirement():
    core = {target["service"] for target in network.TARGETS if target["core"]}
    assert network.classify(observations(core))["core_reachable"] is True
    assert network.classify(observations({"gateway_http"}))["observation"] == (
        "core_services_unreachable_gateway_http_reachable")


class Socket:
    def __init__(self, failure=None, phase="connect"):
        self.calls, self.failure, self.phase = [], failure, phase

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.calls.append(("close",))

    def settimeout(self, timeout):
        self.calls.append(("timeout", timeout))

    def setsockopt(self, *args):
        self.calls.append(("interface", *args))
        if self.phase == "interface" and self.failure:
            raise self.failure

    def bind(self, address):
        self.calls.append(("bind", address))
        if self.phase == "bind" and self.failure:
            raise self.failure

    def connect(self, address):
        self.calls.append(("connect", address))
        if self.failure:
            raise self.failure


def test_forced_interface_is_per_socket_network_order_and_sends_no_application_data():
    connection = Socket()
    result = network.probe_tcp(network.TARGETS[0], 3, 3, "10.20.30.94",
                               socket_factory=lambda *_: connection)
    assert result["reachable"]
    assert connection.calls == [
        ("timeout", 3), ("interface", socket.IPPROTO_IP, 31, struct.pack("!I", 3)),
        ("bind", ("10.20.30.94", 0)), ("connect", ("10.20.30.142", 22)), ("close",)]


@pytest.mark.parametrize(("error", "phase", "expected"), [
    (TimeoutError(), "connect", "timeout"),
    (ConnectionRefusedError(errno.ECONNREFUSED, "private fixture message"), "connect", "connection_refused"),
    (OSError(errno.ENETUNREACH, "private fixture message"), "connect", "network_unreachable"),
    (PermissionError(errno.EACCES, "private fixture message"), "interface", "permission_denied"),
    (OSError(errno.EADDRNOTAVAIL, "private fixture message"), "bind", "source_address_unavailable"),
])
def test_failure_reports_stage_and_safe_error_not_raw_exception(error, phase, expected):
    connection = Socket(error, phase)
    result = network.probe_tcp(network.TARGETS[0], 3, 3, "10.20.30.94",
                               socket_factory=lambda *_: connection)
    assert result["reachable"] is False and result["error"] == expected
    assert result["failure_phase"] == phase and connection.calls[-1] == ("close",)
    assert "private fixture" not in json.dumps(result)


@pytest.mark.parametrize(("timeout", "index", "address", "platform_name", "error"), [
    (float("nan"), None, None, "win32", "TIMEOUT_INVALID"),
    (30, None, None, "win32", "TIMEOUT_INVALID"),
    (3, 3, None, "win32", "INTERFACE_AND_SOURCE_REQUIRED_TOGETHER"),
    (3, None, "10.20.30.94", "win32", "INTERFACE_AND_SOURCE_REQUIRED_TOGETHER"),
    (3, 0, "10.20.30.94", "win32", "INTERFACE_INDEX_INVALID"),
    (3, 3, "::1", "win32", "SOURCE_IPV4_INVALID"),
    (3, 3, "0.0.0.0", "win32", "SOURCE_IPV4_INVALID"),  # noqa: S104 -- verifies rejection
    (3, 3, "not-an-address", "win32", "SOURCE_IPV4_INVALID"),
    (3, 3, "10.20.30.94", "linux", "FORCED_INTERFACE_REQUIRES_WINDOWS"),
])
def test_invalid_interface_and_source_options_stop_before_network(timeout, index, address, platform_name, error):
    def unexpected_probe(*_):
        pytest.fail("invalid arguments reached the network")

    with pytest.raises(network.DiagnosticError, match=error):
        network.diagnose(timeout, index, address, platform_name, probe=unexpected_probe)


def test_forced_diagnosis_retains_default_failures_and_separately_proves_selected_path():
    calls = []

    def probe(target, timeout, index, source):
        calls.append((target["host"], target["port"], timeout, index, source))
        return {**target, "reachable": index == 3, "error": None, "elapsed_ms": 0}

    report = network.diagnose(3, 3, "10.20.30.94", "win32", probe)
    assert report["ok"] is True and report["selected_path"] == "forced_interface"
    assert not report["paths"]["default"]["summary"]["core_reachable"]
    assert report["paths"]["forced_interface"]["summary"]["core_reachable"]
    assert len(calls) == 18
    assert {(item[0], item[1]) for item in calls} == {
        ("10.20.30.142", 22), ("10.20.30.142", 8006), ("10.20.30.45", 22),
        ("10.20.30.45", 8090), ("10.20.30.45", 4007), ("10.20.30.95", 8083),
        ("10.20.30.143", 22), ("10.20.30.143", 8086), ("10.20.30.254", 80)}


@pytest.mark.parametrize("destination", ["report.json", "data/../escaped.json", "../escaped.json",
                                         "data/../data/report.json", "data"])
def test_report_destination_must_be_inside_data_without_traversal(tmp_path, destination):
    with pytest.raises(network.DiagnosticError, match="OUTPUT_PATH_FORBIDDEN"):
        network.output_path(tmp_path, destination)
    assert not list(tmp_path.iterdir())


def test_symlink_report_escape_is_rejected_without_writes(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    data = tmp_path / "project/data"
    data.mkdir(parents=True)
    link = data / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links not available in this Windows test token")
    with pytest.raises(network.DiagnosticError, match="OUTPUT_PATH_FORBIDDEN"):
        network.output_path(tmp_path / "project", "data/linked/report.json")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("reachable", [False, True])
def test_cli_json_exit_code_and_optional_private_report(tmp_path, monkeypatch, capsys, reachable):
    monkeypatch.setattr(network, "probe_tcp", lambda target, *args: {
        **target, "reachable": reachable, "error": None, "elapsed_ms": 0})
    code = network.main(["--output", "data/network.local.json"], project_root=tmp_path)
    report = json.loads(capsys.readouterr().out)
    assert code == (0 if reachable else 1) and report["ok"] is reachable
    saved = tmp_path / "data/network.local.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == report
    if os.name != "nt":
        assert saved.stat().st_mode & 0o777 == 0o600


def test_invalid_output_scope_stops_before_probes(tmp_path, monkeypatch, capsys):
    def unexpected_probe(*_):
        pytest.fail("forbidden output path reached network")

    monkeypatch.setattr(network, "probe_tcp", unexpected_probe)
    assert network.main(["--output", "../../escape.json"], project_root=tmp_path) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "OUTPUT_PATH_FORBIDDEN"}
