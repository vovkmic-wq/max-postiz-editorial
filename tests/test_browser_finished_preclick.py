"""A parent receipt proves only that its exact child ended before the send intent."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from max_postiz_agent import browser_publisher, browser_worker
from max_postiz_agent.config import AgentError, fingerprint
from test_browser_publishing import browser_config


@pytest.fixture
def request_state(project):
    settings = browser_config(project)
    snapshot = {"body": "Точная статья", "title": "Заголовок", "media": [], "metadata": {},
                "platform": "max", "route": "max_browser", "destination": "-123",
                "config_hash": settings.hash}
    return settings, "finished-preclick-request", snapshot


def _paths(settings, request_id):
    data = settings.root / "data"
    return (data / "browser-publications" / (request_id + ".json"),
            data / "browser-publisher-receipts" / (request_id + ".json"))


def _simulate_worker(monkeypatch, request_state, *, output="unknown"):
    settings, request_id, snapshot = request_state
    invocation_ids = []

    def perform(_settings, _payload, _path, _state, **_kwargs):
        if output in {"abnormal_exit", "timeout"}:
            raise KeyboardInterrupt
        return {"state": "unknown"}

    def run(command, *, input, **_kwargs):
        payload = json.loads(input)
        invocation_id = payload.pop(browser_worker.PUBLISHER_INVOCATION_FIELD)
        invocation_ids.append(invocation_id)
        payload["dispatch_snapshot_hash"] = fingerprint(snapshot)
        try:
            result = browser_worker.publish(settings, payload, publisher_invocation_id=invocation_id)
        except KeyboardInterrupt:
            if output == "timeout":
                raise subprocess.TimeoutExpired(command, 30) from None
            return subprocess.CompletedProcess(command, -9, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(browser_worker, "_perform_publish", perform)
    monkeypatch.setattr(browser_publisher.subprocess, "run", run)
    return invocation_ids, lambda: browser_publisher.BrowserMax(settings).publish(
        snapshot["destination"], snapshot["body"], request_id=request_id)


def _read(request_state):
    settings, request_id, snapshot = request_state
    return browser_publisher.read_finished_preclick_receipt(settings, request_id, snapshot=snapshot)


@pytest.mark.parametrize("output", ["unknown", "abnormal_exit", "timeout"])
def test_finished_exact_preparation_proof_stays_unknown_and_is_immutable(request_state, monkeypatch, output):
    settings, request_id, snapshot = request_state
    journal_path, receipt_path = _paths(settings, request_id)
    invocation_ids, publish = _simulate_worker(monkeypatch, request_state, output=output)
    assert publish().state == "unknown"
    journal_raw = journal_path.read_bytes()
    journal = json.loads(journal_raw)
    nonce_field = browser_worker.PUBLISHER_INVOCATION_FIELD
    assert journal["state"] == "preparing" and journal["send_started"] is False
    assert journal[nonce_field] == invocation_ids[0]
    assert nonce_field not in journal["request_payload"]
    if output == "timeout" and os.name != "posix":
        assert not receipt_path.exists()
        with pytest.raises(AgentError, match="BROWSER_FINISHED_PRECLICK_PROOF_INVALID"):
            _read(request_state)
        return
    receipt_raw = receipt_path.read_bytes()
    proof = _read(request_state)
    receipt = proof["receipt"]
    assert receipt["contract"] == browser_publisher.FINISHED_PRECLICK_CONTRACT
    assert receipt["snapshot_hash"] == fingerprint(snapshot)
    assert receipt["completion"] == ("timed_out_reaped" if output == "timeout" else "exited")
    assert proof["sha256"] == hashlib.sha256(receipt_raw).hexdigest()
    assert proof["journal_sha256"] == hashlib.sha256(journal_raw).hexdigest()
    assert receipt["invoked_at"] <= receipt["started_at"] <= receipt["finished_at"]
    assert snapshot["body"] not in receipt_raw.decode("utf-8")
    with pytest.raises(AgentError, match="BROWSER_PRECLICK_PROOF_INVALID"):
        browser_publisher.read_preclick_failure(settings, request_id, snapshot=snapshot)
    monkeypatch.setattr(browser_worker, "_perform_publish", lambda *_a, **_kw: pytest.fail("same request reused"))
    assert publish().state == "unknown"
    assert invocation_ids[0] != invocation_ids[1]
    assert journal_path.read_bytes() == journal_raw
    assert receipt_path.read_bytes() == receipt_raw


@pytest.mark.parametrize("transition", ["prior_invocation", "sending", "unknown", "confirmed", "partial"])
def test_receipt_is_never_minted_for_a_different_or_ambiguous_attempt(request_state, monkeypatch, transition):
    settings, request_id, snapshot = request_state
    journal_path, receipt_path = _paths(settings, request_id)
    _invocations, publish = _simulate_worker(monkeypatch, request_state)
    real_worker = browser_worker.publish

    def competing(_settings, payload, **kwargs):
        if transition == "prior_invocation":
            real_worker(settings, payload, publisher_invocation_id="0" * 32)
            return real_worker(settings, payload, **kwargs)
        return real_worker(settings, payload, **kwargs)

    def perform(_settings, _payload, path, state, **_kwargs):
        if transition == "partial":
            path.with_suffix(".new").write_text('{"state":', encoding="utf-8")
        elif transition != "prior_invocation":
            browser_worker.save(path, {**state, "state": transition, "send_started": True})
        return {"state": "unknown"}

    monkeypatch.setattr(browser_worker, "publish", competing)
    monkeypatch.setattr(browser_worker, "_perform_publish", perform)
    assert publish().state == "unknown"
    assert journal_path.exists()
    assert not receipt_path.exists()
    with pytest.raises(AgentError, match="BROWSER_FINISHED_PRECLICK_PROOF_INVALID"):
        browser_publisher.read_finished_preclick_receipt(settings, request_id, snapshot=snapshot)


@pytest.mark.parametrize("changed", ["journal_bytes", "receipt_nonce", "snapshot", "partial_receipt"])
def test_read_requires_original_receipt_and_exact_journal_bytes(request_state, monkeypatch, changed):
    settings, request_id, snapshot = request_state
    journal_path, receipt_path = _paths(settings, request_id)
    _invocations, publish = _simulate_worker(monkeypatch, request_state)
    assert publish().state == "unknown"
    if changed == "journal_bytes":
        journal_path.write_bytes(journal_path.read_bytes() + b"\n")
    elif changed == "receipt_nonce":
        receipt = json.loads(receipt_path.read_bytes())
        receipt[browser_worker.PUBLISHER_INVOCATION_FIELD] = "0" * 32
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    elif changed == "partial_receipt":
        receipt_path.with_suffix(".new").write_text('{"contract":', encoding="utf-8")
    else:
        snapshot = {**snapshot, "title": "Изменённый снимок"}
    with pytest.raises(AgentError, match="BROWSER_FINISHED_PRECLICK_PROOF_INVALID"):
        browser_publisher.read_finished_preclick_receipt(settings, request_id, snapshot=snapshot)


@pytest.mark.skipif(os.name != "posix", reason="POSIX subprocess.run kill and wait semantics")
def test_real_posix_timeout_does_not_record_completion_until_child_is_reaped(request_state, monkeypatch):
    settings, request_id, snapshot = request_state
    journal_path, _receipt_path = _paths(settings, request_id)
    child = """
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from max_postiz_agent.browser_worker import PUBLISHER_INVOCATION_FIELD, _request_record, save

path = Path(sys.argv[1])
request = json.loads(sys.stdin.read())
invocation = request.pop(PUBLISHER_INVOCATION_FIELD)
request['dispatch_snapshot_hash'] = sys.argv[2]
record = _request_record(SimpleNamespace(hash=request['config_hash']), request)
path.parent.mkdir(parents=True)
save(path, {**record, 'contract': 'browser-send-journal-v1', 'state': 'preparing',
            'send_started': False, 'phase': 'session', 'started_at': time.time(),
            PUBLISHER_INVOCATION_FIELD: invocation}, exclusive=True)
path.with_suffix('.pid').write_text(str(os.getpid()), encoding='ascii')
time.sleep(60)
"""
    actual_run = subprocess.run

    def run(_command, **kwargs):
        kwargs["timeout"] = 4
        command = [sys.executable, "-c", child, str(journal_path), fingerprint(snapshot)]
        return actual_run(command, **kwargs)

    monkeypatch.setattr(browser_publisher.subprocess, "run", run)
    assert browser_publisher.BrowserMax(settings).publish(
        snapshot["destination"], snapshot["body"], request_id=request_id).state == "unknown"
    pid = int(journal_path.with_suffix(".pid").read_text(encoding="ascii"))
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert _read(request_state)["receipt"]["completion"] == "timed_out_reaped"
