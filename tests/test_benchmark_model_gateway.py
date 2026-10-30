"""Диагностика стенда не меняет перевод и не выдаёт усечение за успех."""

import base64
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def gateway(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "deployment/translation"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("benchmark_llama_under_test", directory / "benchmark_llama_gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def translator(gateway, tmp_path, *, audited, finish="stop", output="Телескоп наблюдает звёзды.", status=200):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Translate into Russian:\n{source_text}", encoding="utf-8")
    config = {"prompt_path": str(prompt), "gguf": {"hy-mt2-7b-q8": "http://node.example:8089"},
              "memory_guard": {"model": "hy-mt2-7b-q8", "peer_props_url": "http://node.example:8086/props",
                               "own_props_url": "http://node.example:8089/props", "timeout_seconds": 10, "poll_seconds": 1}}
    instance = (gateway.AuditedTranslator if audited else gateway.Translator)(config)
    instance.http.close()
    calls = []
    payload = {"choices": [{"finish_reason": finish, "message": {"content": output}}],
               "timings": {"predicted_ms": 12}, "usage": {"completion_tokens": 6}}

    def respond(request):
        calls.append((request.method, str(request.url), request.content))
        if request.url.path == "/props":
            return httpx.Response(200, json={"is_sleeping": True})
        return httpx.Response(status, json=payload)

    instance.http = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False,
        event_hooks={"response": [instance.capture]} if audited else {})
    return instance, calls, payload


def test_original_translation_requests_and_guards_are_identical(gateway, tmp_path):
    original, original_calls, _ = translator(gateway, tmp_path, audited=False)
    audited, audited_calls, payload = translator(gateway, tmp_path, audited=True)
    try:
        before = original.translate("hy-mt2-7b-q8", "The telescope observes stars.")
        after = audited.translate("hy-mt2-7b-q8", "The telescope observes stars.")
        assert original_calls == audited_calls
        assert before["text"] == after["text"] and before["complete"] is after["complete"] is True
        assert json.loads(base64.b64decode(after["metrics"]["backend_response"]["raw_base64"])) == payload
    finally:
        original.http.close()
        audited.http.close()


@pytest.mark.parametrize(("finish", "output", "reason"), [("length", "Незавершённый перевод", "length"), ("stop", "", "empty")])
def test_known_generation_failure_keeps_raw_and_never_becomes_complete(gateway, tmp_path, finish, output, reason):
    instance, _, payload = translator(gateway, tmp_path, audited=True, finish=finish, output=output)
    try:
        result = instance.translate("hy-mt2-7b-q8", "The telescope observes stars.")
        assert result["complete"] is False and result["text"] == output
        assert result["metrics"]["known_model_failure"] is True
        assert result["metrics"]["finish_reason"] == reason
        assert json.loads(base64.b64decode(result["metrics"]["backend_response"]["raw_base64"])) == payload
    finally:
        instance.http.close()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_infrastructure_error_is_not_a_known_model_failure(gateway, tmp_path, status):
    instance, _, _ = translator(gateway, tmp_path, audited=True, status=status)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            instance.translate("hy-mt2-7b-q8", "The telescope observes stars.")
    finally:
        instance.http.close()
