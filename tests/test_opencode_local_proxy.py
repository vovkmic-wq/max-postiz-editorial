"""Regression for the inherited proxy that returned 503 for local inference."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.adapters import OpenCode


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


@pytest.mark.parametrize('explicit', [False, True])
def test_managed_credentials_refresh_after_restart(tmp_path, monkeypatch, explicit):
    options = {'adapter_mode':'live', 'base_url_env':'TEST_OC_BASE', 'model_id_env':'TEST_OC_MODEL',
               'auth_secret_env':'TEST_OC_SECRET', 'local_access_path':'access.json',
               'task_timeout_seconds':10, 'required_version':'2.0.17'}
    monkeypatch.delenv('TEST_OC_BASE', raising=False)
    monkeypatch.delenv('TEST_OC_MODEL', raising=False)
    if explicit:
        monkeypatch.setenv('TEST_OC_SECRET', 'explicit-owner-value')
    else:
        monkeypatch.delenv('TEST_OC_SECRET', raising=False)
    path = tmp_path/'access.json'
    path.write_text(json.dumps({'base_url':'http://127.0.0.1:4108','password':'before'}))
    headers=[]

    def handler(request):
        headers.append(request.headers['authorization'])
        return httpx.Response(200,json={'version':'2.0.17'})

    client=OpenCode(SimpleNamespace(root=tmp_path,app={'opencode':options}),httpx.MockTransport(handler))
    assert client.probe()['state']=='available'
    path.write_text(json.dumps({'base_url':'http://127.0.0.1:4108','password':'after'}))
    assert client.probe()['state']=='available'
    import base64
    passwords=[base64.b64decode(x.split()[1]).decode().split(':',1)[1] for x in headers]
    assert passwords==(['explicit-owner-value']*2 if explicit else ['before','after'])
    client.client.close()
