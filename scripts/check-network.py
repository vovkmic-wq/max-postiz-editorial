"""Read-only bounded TCP diagnostics; no authentication, restart or network reconfiguration."""

import argparse
import errno
import ipaddress
import json
import math
import os
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

TARGETS = (
    {"service": "proxmox_ssh", "host": "10.20.30.142", "port": 22, "core": True},
    {"service": "proxmox_api", "host": "10.20.30.142", "port": 8006, "core": True},
    {"service": "editorial_ssh", "host": "10.20.30.45", "port": 22, "core": True},
    {"service": "hub", "host": "10.20.30.45", "port": 8090, "core": True},
    {"service": "postiz", "host": "10.20.30.45", "port": 4007, "core": True},
    {"service": "llm_router", "host": "10.20.30.95", "port": 8083, "core": True},
    {"service": "gpu_ssh", "host": "10.20.30.143", "port": 22, "core": True},
    {"service": "gpu_model", "host": "10.20.30.143", "port": 8086, "core": True},
    {"service": "gateway_http", "host": "10.20.30.254", "port": 80, "core": False},
)
IP_UNICAST_IF = 31


class DiagnosticError(Exception):
    pass


def validate_options(timeout, interface_index, source_ip, platform_name):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 15:
        raise DiagnosticError("TIMEOUT_INVALID")
    if (interface_index is None) != (source_ip is None):
        raise DiagnosticError("INTERFACE_AND_SOURCE_REQUIRED_TOGETHER")
    if interface_index is None:
        return None
    if platform_name != "win32":
        raise DiagnosticError("FORCED_INTERFACE_REQUIRES_WINDOWS")
    if type(interface_index) is not int or not 1 <= interface_index <= 0xFFFFFF:
        raise DiagnosticError("INTERFACE_INDEX_INVALID")
    try:
        address = ipaddress.IPv4Address(source_ip)
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise DiagnosticError("SOURCE_IPV4_INVALID") from exc
    return str(address)


def _socket_error(exc):
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, TimeoutError) or code in {errno.ETIMEDOUT, 10060}:
        return "timeout"
    if code in {errno.ECONNREFUSED, 10061}:
        return "connection_refused"
    if code in {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}:
        return "network_unreachable"
    if code in {errno.EACCES, errno.EPERM, 10013}:
        return "permission_denied"
    if code in {errno.EADDRNOTAVAIL, 10049}:
        return "source_address_unavailable"
    return "socket_error"


def probe_tcp(target, timeout, interface_index=None, source_ip=None, socket_factory=None):
    """Open and close one TCP connection, without sending application data."""
    factory = socket_factory or socket.socket
    started, phase = time.monotonic(), "socket"
    result = {**target, "reachable": False, "error": None}
    try:
        with factory(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            if interface_index is not None:
                phase = "interface"
                connection.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", interface_index))
                phase = "bind"
                connection.bind((source_ip, 0))
            phase = "connect"
            connection.connect((target["host"], target["port"]))
            result["reachable"] = True
    except OSError as exc:
        result.update(error=_socket_error(exc), failure_phase=phase)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def classify(checks):
    states = {item["service"]: item["reachable"] for item in checks}
    core = [item for item in checks if item["core"]]
    core_ok = bool(core) and all(item["reachable"] for item in core)
    proxmox = any(states.get(name, False) for name in ("proxmox_ssh", "proxmox_api"))
    guests = any(states.get(name, False) for name in ("editorial_ssh", "hub", "postiz", "llm_router"))
    gpu = any(states.get(name, False) for name in ("gpu_ssh", "gpu_model"))
    if core_ok:
        observation = "core_services_reachable"
    elif not proxmox and not guests and gpu:
        observation = "proxmox_and_guests_unreachable_gpu_reachable"
    elif any(item["reachable"] for item in core):
        observation = "partial_reachability"
    elif states.get("gateway_http", False):
        observation = "core_services_unreachable_gateway_http_reachable"
    else:
        observation = "tested_endpoints_unreachable"
    return {"core_reachable": core_ok, "observation": observation,
            "power_state": "unconfirmed", "vpn_fault": "unconfirmed",
            "scope": "TCP reachability only; no proof of application health or host power state"}


def diagnose(timeout=3, interface_index=None, source_ip=None, platform_name=None, probe=None):
    platform_name = platform_name or sys.platform
    source_ip = validate_options(timeout, interface_index, source_ip, platform_name)
    check = probe or probe_tcp
    paths = {}
    modes = [("default", None, None)]
    if interface_index is not None:
        modes.append(("forced_interface", interface_index, source_ip))
    for name, index, address in modes:
        with ThreadPoolExecutor(max_workers=len(TARGETS)) as executor:
            checks = list(executor.map(
                lambda target, selected_index=index, selected_address=address:
                check(target, timeout, selected_index, selected_address), TARGETS))
        paths[name] = {"interface_index": index, "source_ip": address,
                       "summary": classify(checks), "checks": checks}
    selected = "forced_interface" if interface_index is not None else "default"
    return {"ok": paths[selected]["summary"]["core_reachable"],
            "checked_at_utc": datetime.now(UTC).isoformat(), "platform": platform_name,
            "timeout_seconds": timeout, "selected_path": selected, "paths": paths}


def output_path(root, value):
    root = Path(root).resolve()
    supplied = Path(value)
    if ".." in supplied.parts or (supplied.drive and not supplied.is_absolute()):
        raise DiagnosticError("OUTPUT_PATH_FORBIDDEN")
    destination = supplied if supplied.is_absolute() else root / supplied
    destination = Path(os.path.abspath(destination))
    directory = root / "data"
    try:
        resolved = destination.resolve()
        if (directory.resolve() != directory or resolved != destination
                or resolved == directory or not resolved.is_relative_to(directory)
                or (destination.exists() and not destination.is_file())):
            raise DiagnosticError("OUTPUT_PATH_FORBIDDEN")
    except (OSError, RuntimeError) as exc:
        raise DiagnosticError("OUTPUT_PATH_FORBIDDEN") from exc
    return destination


def save_report(destination, report):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(".network-check-" + uuid4().hex + ".new")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DiagnosticError("ARGUMENTS_INVALID")


def main(argv=None, project_root=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--interface-index", type=int)
    parser.add_argument("--source-ip")
    parser.add_argument("--output", help="Optional JSON report strictly inside this project's data/")
    try:
        args = parser.parse_args(argv)
        root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[1]
        destination = output_path(root, args.output) if args.output else None
        report = diagnose(args.timeout, args.interface_index, args.source_ip)
        if destination:
            # Recheck immediately before writing as well as before any probes.
            destination = output_path(root, destination)
            save_report(destination, report)
    except (DiagnosticError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, DiagnosticError)
                          else "DIAGNOSTIC_IO_FAILED"}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
