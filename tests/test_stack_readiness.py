"""Backend readiness must use authenticated GETs and must not expose credentials."""
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from max_postiz_agent import postiz

SCRIPT = Path(__file__).resolve().parents[1] / "deployment/linux/check-stack.py"
SPEC = importlib.util.spec_from_file_location("stack_readiness", SCRIPT)
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/publishing.json").write_text(json.dumps({"postiz": {
        "base_url_env": "READINESS_POSTIZ_BASE", "api_key_env": "READINESS_POSTIZ_KEY",
        "public_origin_env": "READINESS_POSTIZ_PUBLIC"}}), encoding="utf-8")
    (tmp_path / ".env.local").write_text(
        "READINESS_POSTIZ_BASE=http://127.0.0.1:4007/api/public/v1\n"
        "READINESS_POSTIZ_KEY=private-test-api-key\n", encoding="utf-8")
    for key in ("READINESS_POSTIZ_BASE", "READINESS_POSTIZ_KEY", "READINESS_POSTIZ_PUBLIC"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


@pytest.mark.parametrize("status,expected", [
    (503, "POSTIZ_UNAVAILABLE"), (401, "POSTIZ_ACCESS_DENIED"),
])
def test_unready_or_unauthorized_backend_fails_closed_without_secrets(
        root, monkeypatch, capsys, status, expected):
    seen, instances = [], []
    real = postiz.PostizClient

    def request(value):
        seen.append((value.method, value.url.path))
        assert value.headers["Authorization"] == "private-test-api-key"
        return httpx.Response(status, json={"private": "private-test-api-key"})

    def client(options):
        value = real(options, transport=httpx.MockTransport(request))
        instances.append(value)
        return value

    monkeypatch.setattr(postiz, "PostizClient", client)
    monkeypatch.setattr(stack, "http", lambda *_: pytest.fail("Public-page fallback is forbidden"))
    result = stack.postiz_api(root)
    assert result == (False, expected)
    assert seen == [("GET", "/api/public/v1/posts")]
    assert instances[0].client.is_closed
    assert "private-test-api-key" not in json.dumps(result) + capsys.readouterr().out


def test_success_loads_private_env_and_reads_posts_once_without_mutation_or_fallback(root, monkeypatch):
    seen = []
    real = postiz.PostizClient

    def request(value):
        seen.append((value.method, value.url.path))
        assert value.headers["Authorization"] == "private-test-api-key"
        return httpx.Response(200, json={"posts": [{"id": "private-post-id"}]})

    monkeypatch.setattr(postiz, "PostizClient",
        lambda options: real(options, transport=httpx.MockTransport(request)))
    monkeypatch.setattr(stack, "http", lambda *_: pytest.fail("Public-page fallback is forbidden"))
    assert stack.postiz_api(root) == (True, "authenticated GET /posts posts=1")
    assert seen == [("GET", "/api/public/v1/posts")]


def test_unknown_failure_hides_exception_body_and_missing_config_does_not_fallback(root, monkeypatch):
    def fail(_):
        raise ValueError("private-test-api-key private configuration contents")

    monkeypatch.setattr(postiz, "PostizClient", fail)
    assert stack.postiz_api(root) == (False, "ValueError")
    (root / "config/publishing.json").unlink()
    assert stack.postiz_api(root) == (False, "FileNotFoundError")


def test_missing_app_import_is_a_readiness_failure_without_private_traceback(root, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "max_postiz_agent.cli":
            raise ImportError("private local interpreter configuration")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert stack.postiz_api(root) == (False, "ImportError")


def test_default_checker_includes_translation_and_authenticated_api_without_inference(monkeypatch, capsys):
    commands, urls = [], []

    def command(*args, **_):
        commands.append(args)
        if args[-1] == "max-postiz-hub.service":
            return False, "disabled" if args[1] == "is-enabled" else "inactive"
        if args[:2] == ("systemctl", "is-enabled"):
            return True, "enabled"
        if args[:2] == ("systemctl", "is-active"):
            return True, "active"
        return True, json.dumps({"Status": "running", "Health": {"Status": "healthy"}})

    def http(url):
        urls.append(url)
        if url == "http://127.0.0.1:8092/health":
            return True, "HTTP 200", json.dumps({"status": "ready", "interface": "postiz_only",
                                                "worker_mode": "autonomous"}).encode()
        return True, "HTTP 200", json.dumps({"data": [
            {"id": "qwen35-9b"}, {"id": "qwen25-coder-7b"}]}).encode()

    monkeypatch.setattr(stack, "command", command)
    monkeypatch.setattr(stack, "http", http)
    monkeypatch.setattr(stack, "tcp", lambda *_: (True, "connected"))
    monkeypatch.setattr(stack, "postiz_api", lambda _: (False, "POSTIZ_UNAVAILABLE"))
    monkeypatch.setattr(stack.sys, "argv", [str(SCRIPT)])
    assert stack.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert len(report["checks"]) == 29 and report["ok"] is False
    assert report["checks"]["api:postiz_posts"] == {"ok": False, "detail": "POSTIZ_UNAVAILABLE"}
    assert report["checks"]["service:max-postiz-translation.service"]["ok"] is True
    assert report["checks"]["http:translation"]["ok"] is True
    assert report["checks"]["http:editorial"]["ok"] is True
    assert report["checks"]["retired:max-postiz-hub.service"]["ok"] is True
    assert report["checks"]["service:max-postiz-editorial.service"]["ok"] is True
    assert not any(":8090" in url or ":8091" in url for url in urls)
    assert "http://127.0.0.1:8094/health" in urls
    assert not any("inference:" in name for name in report["checks"])
    assert ("systemctl", "is-active", "max-postiz-translation.service") in commands


@pytest.mark.parametrize("payload", [
    {"status": "ready", "interface": "postiz_only", "worker_mode": "api_only"},
    {"status": "ready", "interface": "postiz_only"},
    {"status": "ready", "interface": "hub", "worker_mode": "autonomous"},
    {"status": "starting", "interface": "postiz_only", "worker_mode": "autonomous"},
    [], None,
])
def test_http_200_without_autonomous_editorial_process_is_not_ready(monkeypatch, payload):
    monkeypatch.setattr(stack, "http", lambda _: (True, "HTTP 200", json.dumps(payload).encode()))
    assert stack.editorial_health() == (False, "EDITORIAL_AUTONOMOUS_REQUIRED")


def test_malformed_or_unreachable_editorial_health_is_not_ready(monkeypatch):
    monkeypatch.setattr(stack, "http", lambda _: (True, "HTTP 200", b"not-json"))
    assert stack.editorial_health() == (False, "EDITORIAL_HEALTH_INVALID")
    monkeypatch.setattr(stack, "http", lambda _: (False, "URLError", b""))
    assert stack.editorial_health() == (False, "URLError")


@pytest.mark.parametrize("enabled,active,accepted", [
    ("disabled", "inactive", True), ("masked", "inactive", True),
    ("not-found", "unknown", True), ("enabled", "inactive", False),
    ("enabled-runtime", "inactive", False), ("disabled", "active", False),
    ("disabled", "activating", False), ("", "", False),
])
def test_old_hub_must_remain_stopped_and_cannot_autostart(
        monkeypatch, capsys, enabled, active, accepted):
    commands = []

    def command(*args, **_):
        commands.append(args)
        if args[-1] == "max-postiz-hub.service":
            return False, enabled if args[1] == "is-enabled" else active
        if args[:2] == ("systemctl", "is-enabled"):
            return True, "enabled"
        if args[:2] == ("systemctl", "is-active"):
            return True, "active"
        return True, json.dumps({"Status": "running", "Health": {"Status": "healthy"}})

    def http(url):
        data = ({"status": "ready", "interface": "postiz_only", "worker_mode": "autonomous"}
                if ":8092/" in url else {"data": [{"id": "qwen35-9b"}, {"id": "qwen25-coder-7b"}]})
        return True, "HTTP 200", json.dumps(data).encode()

    monkeypatch.setattr(stack, "command", command)
    monkeypatch.setattr(stack, "http", http)
    monkeypatch.setattr(stack, "tcp", lambda *_: (True, "connected"))
    monkeypatch.setattr(stack, "postiz_api", lambda _: (True, "authenticated GET /posts posts=0"))
    monkeypatch.setattr(stack.sys, "argv", [str(SCRIPT)])
    assert stack.main() == (0 if accepted else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["retired:max-postiz-hub.service"]["ok"] is accepted
    assert all(args[1] in {"is-enabled", "is-active"} for args in commands if args[0] == "systemctl")
