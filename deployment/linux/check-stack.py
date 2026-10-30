#!/usr/bin/env python3
"""Read-only boot/runtime checks for the editorial VM and its model peers."""

import argparse
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


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


def editorial_health() -> tuple[bool, str]:
    """HTTP 200 у временного API-only не доказывает работу автономного процесса."""
    ok, detail, raw = http("http://127.0.0.1:8092/health")
    if not ok:
        return False, detail
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False, "EDITORIAL_HEALTH_INVALID"
    if (not isinstance(value, dict) or value.get("status") != "ready"
            or value.get("interface") != "postiz_only"
            or value.get("worker_mode") != "autonomous"):
        return False, "EDITORIAL_AUTONOMOUS_REQUIRED"
    return True, "ready postiz_only autonomous"


def postiz_api(root: Path) -> tuple[bool, str]:
    """Exercise the configured authenticated read API without constructing the Hub/DB."""
    safe_errors = {
        "POSTIZ_NOT_CONFIGURED", "POSTIZ_PUBLIC_ORIGIN_INVALID", "POSTIZ_BASE_URL_INVALID",
        "POSTIZ_HTTPS_REQUIRED", "POSTIZ_UNAVAILABLE", "POSTIZ_ACCESS_DENIED",
        "POSTIZ_RATE_LIMIT", "POSTIZ_REJECTED", "POSTIZ_RESPONSE_INVALID",
    }
    try:
        # Import lazily so a wrong interpreter or absent app is a failed readiness check.
        from max_postiz_agent.cli import load_env
        from max_postiz_agent.postiz import PostizClient

        config = json.loads((root / "config/publishing.json").read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or not isinstance(config.get("postiz"), dict):
            raise ValueError("Publishing configuration is missing Postiz options")
        load_env(root)
        client = PostizClient(config["postiz"])
        try:
            count = len(client.posts())
        finally:
            client.close()
        return True, f"authenticated GET /posts posts={count}"
    except Exception as exc:
        # Never stringify exceptions: their text can contain credentials or private bodies.
        code = getattr(exc, "code", None)
        return False, code if isinstance(code, str) and code in safe_errors else type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true",
                        help="also run one real short completion on each model")
    args = parser.parse_args()
    report: dict[str, dict[str, object]] = {}

    def record(name: str, ok: bool, detail: str) -> None:
        report[name] = {"ok": ok, "detail": detail}

    for unit in (
        "docker.service", "nginx.service", "max-postiz-editorial.service",
        "max-postiz-opencode.service", "max-postiz-spelling.service",
        "max-postiz-display.service", "max-postiz-vnc.service",
        "max-postiz-novnc.service", "max-postiz-translation.service",
    ):
        enabled, enabled_value = command("systemctl", "is-enabled", unit)
        active, active_value = command("systemctl", "is-active", unit)
        record(f"service:{unit}", enabled and enabled_value == "enabled"
               and active and active_value == "active",
               f"enabled={enabled_value or '?'} active={active_value or '?'}")

    # Старый Hub не должен самозапуститься рядом с единственным новым worker.
    _, old_enabled = command("systemctl", "is-enabled", "max-postiz-hub.service")
    _, old_active = command("systemctl", "is-active", "max-postiz-hub.service")
    record("retired:max-postiz-hub.service",
           old_enabled in {"disabled", "masked", "not-found"} and old_active in {"inactive", "unknown"},
           f"enabled={old_enabled or '?'} active={old_active or '?'}")

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
        ("postiz", "http://10.20.30.45:4007/auth/login"),
        ("opencode", "http://127.0.0.1:4108/global/health"),
        ("spelling", "http://127.0.0.1:8010/v2/languages"),
        ("model_router", "http://10.20.30.95:8083/health"),
        ("ministral_backend", "http://10.20.30.143:8086/health"),
        ("translation", "http://127.0.0.1:8094/health"),
    ):
        ok, detail, _ = http(url)
        record(f"http:{name}", ok, detail)

    ok, detail = editorial_health()
    record("http:editorial", ok, detail)

    ok, detail = postiz_api(Path(__file__).resolve().parents[2])
    record("api:postiz_posts", ok, detail)

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
