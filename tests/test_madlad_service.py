"""Production gateway не подменяет известный отказ успехом и не повторяет unknown."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def service():
    path = Path(__file__).resolve().parents[1] / "deployment/translation/madlad_service.py"
    spec = importlib.util.spec_from_file_location("madlad_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configured(service, tmp_path, monkeypatch):
    artifacts, hashes = {}, {}
    for name in service.ARTIFACT_SHA256:
        path = tmp_path / name
        path.write_bytes((name + "-fixed-test-artifact").encode())
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts[name] = {"path": str(path), "sha256": hashes[name]}
    monkeypatch.setattr(service, "ARTIFACT_SHA256", hashes)
    raw = {"schema_version": "madlad-service-v1", "model": service.MODEL,
           "artifacts": artifacts, "state_dir": str(tmp_path / "data/runtime"),
           "resources": {"min_available_mb": 6144, "peer_wait_seconds": 1,
                         "request_timeout_seconds": 2, "poll_seconds": 0.1}}
    path = tmp_path / "service.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    memory = tmp_path / "meminfo"
    memory.write_text("MemAvailable: 9000000 kB\n", encoding="utf-8")
    return service.load_config(path), raw, path, memory


class Pipe(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class Process:
    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid, self.returncode = 12345, None
        self.stdin, self.stdout = Pipe(), Pipe()
        self.terminated, self.killed = False, False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def kill(self):
        self.killed, self.returncode = True, -9

    def wait(self, **_kwargs):
        return self.returncode


def response(*, text="Телескоп наблюдает звёзды.", complete=True, reason="EOS", ids=None):
    return {"text": text, "complete": complete, "finish_reason": reason,
            "source_tokens": 10, "output_tokens": len(ids if ids is not None else [41, 42, 2]),
            "token_ids": ids if ids is not None else [41, 42, 2], "load_seconds": 1.5,
            "inference_seconds": 3.2, "cold_start": True}


def request(service, text="The telescope observes stars."):
    return {"model": service.MODEL, "text": text, "source": "en", "target": "ru"}


def translator(service, configured, monkeypatch, *, value=None, read_error=None, peers=True):
    config, _raw, _path, memory = configured
    created, calls = [], []

    def factory(args, **kwargs):
        item = Process(args, **kwargs)
        created.append(item)
        return item

    def peer(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"is_sleeping": peers})

    instance = service.Translator(config, client=httpx.Client(transport=httpx.MockTransport(peer)),
                                  process_factory=factory, memory_path=memory)
    raw = (json.dumps(response() if value is None else value, ensure_ascii=False) + "\n").encode()

    def read(process, deadline, directory):
        assert deadline > 0
        assert process.stdin.getvalue() == json.dumps(
            {"text": request(service)["text"], "max_new_tokens": 1024}, ensure_ascii=False).encode() + b"\n"
        if read_error:
            raise service.ServiceError(read_error)
        return raw

    monkeypatch.setattr(instance, "_read_response", read)
    return instance, created, calls, raw


def test_config_pins_four_artifacts_before_any_inference(service, configured):
    config, _raw, _path, _memory = configured
    assert set(config["paths"]) == {"worker", "model", "tokenizer", "config"}
    assert all(path.is_file() for path in config["paths"].values())


@pytest.mark.parametrize("artifact", ["worker", "model", "tokenizer", "config"])
def test_mismatched_artifact_is_rejected(service, configured, artifact):
    _config, raw, path, _memory = configured
    Path(raw["artifacts"][artifact]["path"]).write_bytes(b"old-or-unverified-file")
    with pytest.raises(service.ServiceError, match="MADLAD_ARTIFACT_HASH_MISMATCH"):
        service.load_config(path)


def test_config_cannot_replace_pinned_hash_or_model(service, configured):
    _config, raw, path, _memory = configured
    raw["artifacts"]["worker"]["sha256"] = "0" * 64
    path.write_text(json.dumps(raw))
    with pytest.raises(service.ServiceError, match="MADLAD_CONFIG_INVALID"):
        service.load_config(path)
    raw["model"] = "madlad400-3b-q4k"
    path.write_text(json.dumps(raw))
    with pytest.raises(service.ServiceError, match="MADLAD_CONFIG_INVALID"):
        service.load_config(path)


@pytest.mark.parametrize("outer", ["", "data/release/data/pytest"])
def test_private_state_cannot_escape_data_by_parent_components(service, configured, tmp_path, outer):
    _config, raw, path, _memory = configured
    proposed = tmp_path / outer / "data/../outside"
    assert ".." in proposed.parts
    if outer:
        assert "data" in proposed.resolve().parts
    raw["state_dir"] = str(proposed)
    path.write_text(json.dumps(raw))
    with pytest.raises(service.ServiceError, match="MADLAD_CONFIG_INVALID"):
        service.load_config(path)


def test_clean_nested_data_state_is_supported(service, configured, tmp_path):
    _config, raw, path, _memory = configured
    proposed = tmp_path / "data/release/data/runtime"
    raw["state_dir"] = str(proposed)
    path.write_text(json.dumps(raw))
    assert service.load_config(path)["state"] == proposed.resolve()


def test_exact_output_raw_and_identity_return_only_after_owned_process_stops(service, configured, monkeypatch):
    instance, created, calls, raw = translator(service, configured, monkeypatch)
    try:
        result = instance.translate(request(service))
        assert len(created) == 1 and created[0].terminated and created[0].stdin.closed
        assert instance.process is None
        assert result["text"] == response()["text"] and result["complete"] is True
        assert result["metrics"]["artifact_sha256"] == service.ARTIFACT_SHA256
        assert result["metrics"]["backend_contract"] == service.CONTRACT
        assert result["metrics"]["memory_guard"]["released_before_response"] is True
        assert result["metrics"]["token_ids"] == [41, 42, 2]
        assert base64.b64decode(result["metrics"]["raw_worker_base64"]) == raw
        assert result["metrics"]["raw_worker_sha256"] == hashlib.sha256(raw).hexdigest()
        directory = next(instance.requests.iterdir())
        assert json.loads((directory / "claim.local.json").read_text())["request"] == request(service)
        assert json.loads((directory / "terminal.local.json").read_text(encoding="utf-8"))["result"] == result
        assert calls == list(service.PEER_PROPS)
        assert created[0].kwargs["env"]["RAYON_NUM_THREADS"] == "4"
    finally:
        instance.close()


@pytest.mark.parametrize("case", ["length", "empty"])
def test_known_failure_preserves_raw_without_whole_service_quarantine(service, configured, monkeypatch, case):
    value = response(complete=False, reason="length", ids=[41] * 1024) if case == "length" else response(text="", ids=[2])
    instance, created, _calls, raw = translator(service, configured, monkeypatch, value=value)
    try:
        result = instance.translate(request(service))
        assert result["complete"] is False
        assert result["metrics"]["finish_reason"] == case
        assert result["metrics"]["known_model_failure"] is True
        assert base64.b64decode(result["metrics"]["raw_worker_base64"]) == raw
        assert not instance.quarantine_path.exists() and created[0].terminated
        assert instance.health()[0] == 200
    finally:
        instance.close()


@pytest.mark.parametrize("change", ["wrong_eos", "bool_string", "token_count", "nan", "extra"])
def test_invalid_protocol_is_unknown_and_blocks_next_request(service, configured, monkeypatch, change):
    value = response()
    if change == "wrong_eos":
        value["token_ids"][-1] = 3
    elif change == "bool_string":
        value["complete"] = "true"
    elif change == "token_count":
        value["output_tokens"] = 4
    elif change == "nan":
        value["inference_seconds"] = float("nan")
    else:
        value["approved"] = True
    instance, created, _calls, raw = translator(service, configured, monkeypatch, value=value)
    try:
        with pytest.raises(service.ServiceError, match="MADLAD_RESPONSE_INVALID"):
            instance.translate(request(service))
        assert created[0].terminated and instance.quarantine_path.exists()
        directory = next(instance.requests.iterdir())
        assert base64.b64decode(json.loads((directory / "raw.local.json").read_text(encoding="utf-8"))["base64"]) == raw
        with pytest.raises(service.ServiceError, match="MADLAD_RECONCILIATION_REQUIRED"):
            instance.translate(request(service))
        assert len(created) == 1
    finally:
        instance.close()


@pytest.mark.parametrize("code", ["MADLAD_WORKER_TIMEOUT", "MADLAD_WORKER_EXITED", "MADLAD_CAPACITY_INTERRUPTED"])
def test_unknown_stops_only_owned_worker_and_survives_gateway_restart(service, configured, monkeypatch, code):
    instance, created, _calls, _raw = translator(service, configured, monkeypatch, read_error=code)
    with pytest.raises(service.ServiceError, match=code):
        instance.translate(request(service))
    assert created[0].terminated and len(created) == 1
    instance.close()
    restarted = service.Translator(configured[0], client=httpx.Client(transport=httpx.MockTransport(
        lambda _req: pytest.fail("Quarantine must not even query peers"))), memory_path=configured[3])
    try:
        with pytest.raises(service.ServiceError, match="MADLAD_RECONCILIATION_REQUIRED"):
            restarted.translate(request(service))
        assert restarted.health()[0] == 503
    finally:
        restarted.close()


def test_health_does_not_load_model_or_query_inference(service, configured, monkeypatch):
    instance, created, calls, _raw = translator(service, configured, monkeypatch)
    try:
        status, value = instance.health()
        assert status == 200 and value["status"] == "configured"
        assert value["inference_verified"] is False and value["model_loaded"] is False
        assert calls == created == []
    finally:
        instance.close()


def test_invalid_input_and_busy_do_not_create_attempts(service, configured, monkeypatch):
    instance, created, calls, _raw = translator(service, configured, monkeypatch)
    try:
        for value in [request(service, ""), request(service, "x" * 1601), {**request(service), "model": "other"}]:
            with pytest.raises(service.ServiceError, match="INPUT_INVALID"):
                instance.translate(value)
        instance.lock.acquire()
        try:
            with pytest.raises(service.ServiceError, match="BUSY"):
                instance.translate(request(service))
        finally:
            instance.lock.release()
        assert list(instance.requests.iterdir()) == [] and calls == created == []
    finally:
        instance.close()


def test_changed_artifact_after_startup_blocks_before_process_creation(service, configured, monkeypatch):
    instance, created, calls, _raw = translator(service, configured, monkeypatch)
    try:
        configured[0]["paths"]["worker"].write_bytes(b"replacement")
        with pytest.raises(service.ServiceError, match="MADLAD_ARTIFACT_CHANGED"):
            instance.translate(request(service))
        assert created == calls == [] and instance.health()[0] == 503
    finally:
        instance.close()


def test_unfinished_claim_is_not_implicitly_retried(service, configured):
    state = configured[0]["state"]
    pending = state / "requests/unfinished"
    pending.mkdir(parents=True)
    (pending / "claim.local.json").write_text("{}")
    instance = service.Translator(configured[0])
    try:
        assert instance.health()[0] == 503
        assert json.loads(instance.quarantine_path.read_text())["code"] == "MADLAD_PRIOR_REQUEST_UNRESOLVED"
    finally:
        instance.close()


def test_successful_request_remains_resolved_on_restart(service, configured, monkeypatch):
    instance, _created, _calls, _raw = translator(service, configured, monkeypatch)
    instance.translate(request(service))
    instance.close()
    restarted = service.Translator(configured[0])
    try:
        assert restarted.health()[0] == 200 and not restarted.quarantine_path.exists()
    finally:
        restarted.close()


def test_partial_terminal_file_does_not_hide_unresolved_request(service, configured):
    pending = configured[0]["state"] / "requests/unfinished"
    pending.mkdir(parents=True)
    (pending / "claim.local.json").write_text("{}")
    (pending / "terminal.local.json").write_text('{"outcome":')
    instance = service.Translator(configured[0])
    try:
        assert instance.health()[0] == 503 and instance.quarantine_path.exists()
    finally:
        instance.close()


@pytest.mark.skipif(os.name != "posix", reason="Native worker uses Linux pipe selector")
def test_real_partial_pipe_response_survives_timeout(service, configured, monkeypatch, tmp_path):
    instance = service.Translator(configured[0])
    monkeypatch.setattr(instance, "_capacity", lambda: None)
    process = subprocess.Popen([sys.executable, "-u", "-c",
        "import sys,time;sys.stdout.buffer.write(b'partial-json');sys.stdout.flush();time.sleep(5)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        with pytest.raises(service.ServiceError, match="MADLAD_WORKER_TIMEOUT"):
            instance._read_response(process, time.monotonic() + 0.3, tmp_path)
        assert (tmp_path / "worker-stream.local.bin").read_bytes() == b"partial-json"
    finally:
        instance._stop(process)
        instance.close()


@pytest.mark.skipif(os.name != "posix", reason="Native worker uses Linux pipe selector")
def test_real_pipe_complete_response_is_not_truncated(service, configured, monkeypatch, tmp_path):
    instance = service.Translator(configured[0])
    monkeypatch.setattr(instance, "_capacity", lambda: None)
    raw = (json.dumps(response(), ensure_ascii=False) + "\n").encode()
    process = subprocess.Popen([sys.executable, "-u", "-c",
        "import sys,time;sys.stdout.buffer.write(" + repr(raw) + ");sys.stdout.flush();time.sleep(5)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        observed = instance._read_response(process, time.monotonic() + 2, tmp_path)
        assert observed == raw == (tmp_path / "worker-stream.local.bin").read_bytes()
        assert instance._validate(observed)["text"] == response()["text"]
    finally:
        instance._stop(process)
        instance.close()


def test_peer_busy_expires_without_starting_model_or_claim(service, configured, monkeypatch):
    instance, created, calls, _raw = translator(service, configured, monkeypatch, peers=False)
    clock = [0.0]
    instance.clock = lambda: clock[0]
    instance.sleep = lambda delta: clock.__setitem__(0, clock[0] + delta)
    try:
        with pytest.raises(service.ServiceError, match="MADLAD_RESOURCE_WAIT_TIMEOUT"):
            instance.translate(request(service))
        assert not list(instance.requests.iterdir()) and created == [] and calls
        assert not instance.quarantine_path.exists()
    finally:
        instance.close()


def test_http_health_and_validation_never_invoke_worker(service, configured, monkeypatch):
    instance, created, _calls, _raw = translator(service, configured, monkeypatch)
    class TestHandler(service.Handler):
        translator = instance
    server = service.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), trust_env=False) as client:
            assert client.get("/health").json()["inference_verified"] is False
            assert client.post("/translate", json={**request(service), "model": "old-madlad"}).status_code == 400
            assert client.post("/other", json=request(service)).status_code == 404
        assert created == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        instance.close()
