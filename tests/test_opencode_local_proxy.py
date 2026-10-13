"""Regression for the inherited proxy that returned 503 for local inference."""
import importlib.util
import json
from pathlib import Path


def test_local_provider_bypasses_proxy_without_changing_public_proxy(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/serve_opencode.py"
    spec = importlib.util.spec_from_file_location("serve_opencode", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "opencode.json").write_text(json.dumps({"providers": {
        "local": {"settings": {"baseURL": "http://10.20.30.95:8083/v1"}},
        "public": {"settings": {"baseURL": "https://api.example.com/v1"}},
    }}), encoding="utf-8")
    original = {"HTTP_PROXY": "http://127.0.0.1:10809", "NO_PROXY": "existing.internal",
                "no_proxy": "lower.internal"}
    result = module.local_provider_environment(tmp_path, original)
    entries = set(result["no_proxy"].split(","))
    assert {"10.20.30.95", "127.0.0.1", "localhost", "existing.internal", "lower.internal"} <= entries
    assert "api.example.com" not in entries
    assert result["HTTP_PROXY"] == original["HTTP_PROXY"]
    assert "10.20.30.95" not in original["NO_PROXY"]
