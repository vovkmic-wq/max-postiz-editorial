#!/usr/bin/env python3
"""Read-only boot/runtime checks for the editorial VM and its model peers."""

import argparse
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def command(*args: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    return completed.returncode == 0, completed.stdout.strip()


def http(url: str, timeout: int = 8) -> tuple[bool, str, bytes]:
    try:
        # The owner's workstation may have an unrelated proxy. These are LAN URLs.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            body = response.read(131072)
            return response.status == 200, f"HTTP {response.status}", body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, type(exc).__name__, b""


def tcp(host: str, port: int) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True, "connected"
    except OSError as exc:
        return False, type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true",
                        help="also run one real short completion on each model")
    args = parser.parse_args()
    report: dict[str, dict[str, object]] = {}

    def record(name: str, ok: bool, detail: str) -> None:
        report[name] = {"ok": ok, "detail": detail}

    for unit in (
        "docker.service", "nginx.service", "max-postiz-hub.service",
        "max-postiz-opencode.service", "max-postiz-spelling.service",
        "max-postiz-display.service", "max-postiz-vnc.service",
        "max-postiz-novnc.service",
    ):
        enabled, enabled_value = command("systemctl", "is-enabled", unit)
        active, active_value = command("systemctl", "is-active", unit)
        record(f"service:{unit}", enabled and enabled_value == "enabled"
               and active and active_value == "active",
               f"enabled={enabled_value or '?'} active={active_value or '?'}")

    expected_containers = (
        "max-postiz-hub-postgres-1", "max-postiz-postiz-postgres-1",
        "max-postiz-postiz-redis-1", "max-postiz-temporal-postgresql-1",
        "max-postiz-temporal-elasticsearch-1", "max-postiz-temporal-1",
        "max-postiz-temporal-ui-1", "max-postiz-postiz-1",
    )
    for name in expected_containers:
        ok, raw = command("sudo", "-n", "docker", "inspect", "--format",
                          "{{json .State}}", name)
        try:
            state = json.loads(raw) if ok else {}
            running = state.get("Status") == "running"
            health = state.get("Health", {}).get("Status", "none")
            record(f"container:{name}", running and health == "healthy",
                   f"state={state.get('Status', 'missing')} health={health}")
        except json.JSONDecodeError:
            record(f"container:{name}", False, "Docker state unavailable")

    for name, url in (
        ("hub", "http://10.20.30.45:8090/hub"),
        ("postiz", "http://10.20.30.45:4007/auth/login"),
        ("opencode", "http://127.0.0.1:4108/global/health"),
        ("spelling", "http://127.0.0.1:8010/v2/languages"),
        ("model_router", "http://10.20.30.95:8083/health"),
        ("ministral_backend", "http://10.20.30.143:8086/health"),
    ):
        ok, detail, _ = http(url)
        record(f"http:{name}", ok, detail)

    ok, detail, body = http("http://10.20.30.95:8083/v1/models")
    try:
        model_ids = {entry["id"] for entry in json.loads(body)["data"]} if ok else set()
        needed = {"qwen35-9b", "qwen25-coder-7b"}
        record("models", needed <= model_ids,
               "available=" + ",".join(sorted(model_ids)))
    except (ValueError, KeyError, TypeError):
        record("models", False, detail + " invalid catalog")

    for name, host, port in (("proxmox", "10.20.30.142", 8006),
                             ("editorial_ssh", "10.20.30.45", 22)):
        connected, detail = tcp(host, port)
        record(f"tcp:{name}", connected, detail)

    if args.deep:
        for model in ("qwen35-9b", "qwen25-coder-7b"):
            request_body = {
                "model": model, "stream": False, "temperature": 0,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            }
            if model == "qwen35-9b":
                # Otherwise the small token budget is consumed by hidden reasoning.
                request_body["chat_template_kwargs"] = {"enable_thinking": False}
            payload = json.dumps(request_body).encode()
            request = urllib.request.Request(
                "http://10.20.30.95:8083/v1/chat/completions", payload,
                {"Content-Type": "application/json"}, method="POST")
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=150) as response:
                    result = json.load(response)
                choice = result.get("choices", [{}])[0]
                content = choice.get("message", {}).get("content", "")
                complete = bool(content) and choice.get("finish_reason") == "stop"
                record(f"inference:{model}", complete,
                       "completion received" if complete else "empty or truncated completion")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                record(f"inference:{model}", False, type(exc).__name__)

    print(json.dumps({"ok": all(item["ok"] for item in report.values()),
                      "checks": report}, ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
