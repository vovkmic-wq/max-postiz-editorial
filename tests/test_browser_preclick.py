"""Новый отказ до отправки требует полного журнала; старый unknown остаётся заблокирован."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from max_postiz_agent import browser_worker
from max_postiz_agent.browser_publisher import BrowserMax, read_preclick_failure
from max_postiz_agent.config import AgentError, fingerprint
from test_browser_publishing import browser_config


@pytest.fixture
def future_request(project):
    settings = browser_config(project)
    snapshot = {"body": "Точная будущая статья", "title": "Заголовок", "media": [], "metadata": {},
        "platform": "max", "route": "max_browser", "destination": "-123", "config_hash": settings.hash}
    payload = {"action": "publish", "request_id": "future-future_request", "chat_id": "-123",
        "text": snapshot["body"], "attachment": None, "config_hash": settings.hash,
        "dispatch_snapshot_hash": fingerprint(snapshot)}
    return settings, payload, snapshot


def fail_before_session(monkeypatch, code="BROWSER_START_FAILED"):
    @contextmanager
    def failed(*_args):
        raise AgentError(code)
        yield

    monkeypatch.setattr(browser_worker, "session", failed)


def terminal(future_request, monkeypatch):
    settings, payload, _snapshot = future_request
    fail_before_session(monkeypatch)
    result = browser_worker.publish(settings, payload)
    assert result["state"] == "preclick_failed"
    path = browser_worker.ledger_path(settings, payload["request_id"])
    return path, result


def test_terminal_worker_failure_is_exact_private_and_never_reopens_browser(future_request, monkeypatch):
    settings, payload, snapshot = future_request
    private_message = "private page text and opaque authentication state"

    @contextmanager
    def failed(*_args):
        raise RuntimeError(private_message)
        yield

    monkeypatch.setattr(browser_worker, "session", failed)
    result = browser_worker.publish(settings, payload)
    proof = read_preclick_failure(settings, payload["request_id"], snapshot=snapshot, chat_id="-123")
    assert proof["sha256"] == result["proof_sha256"]
    assert proof["proof"]["error"] == {"code": "BROWSER_OPERATION_FAILED", "type": "RuntimeError",
        "message": private_message}
    assert proof["proof"]["phase"] == "session" and proof["proof"]["send_started"] is False
    assert private_message not in json.dumps(result)
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("future_request reopened"))
    assert browser_worker.publish(settings, payload) == result


def test_publisher_raises_only_after_valid_terminal_result_and_matching_file_sha(future_request, monkeypatch):
    settings, payload, _snapshot = future_request
    _path, result = terminal(future_request, monkeypatch)
    publisher = BrowserMax(settings)
    monkeypatch.setattr(publisher, "call", lambda *_args, **_kwargs: result)
    with pytest.raises(AgentError, match="^BROWSER_PRECLICK_FAILED$"):
        publisher.publish("-123", payload["text"], request_id=payload["request_id"])
    assert publisher.publish("-123", "Другой текст", request_id=payload["request_id"]).state == "unknown"
    assert publisher.publish("-456", payload["text"], request_id=payload["request_id"]).state == "unknown"
    monkeypatch.setattr(publisher, "call", lambda *_args, **_kwargs: {**result, "proof_sha256": "0" * 64})
    assert publisher.publish("-123", payload["text"], request_id=payload["request_id"]).state == "unknown"


@pytest.mark.parametrize("change", ["sending", "unknown", "legacy", "send_started", "false_integer",
    "future_request", "config", "body_hash", "payload_hash", "attachment", "phase", "missing_error",
    "snapshot_hash", "presentation", "delivery", "receipt", "permalink", "payload_send_started",
    "timestamp_boolean", "timestamp_nan", "partial_write", "invalid_json", "duplicate_json_key"])
def test_proof_reader_rejects_incomplete_changed_or_nonterminal_evidence(future_request, monkeypatch, change):
    settings, payload, snapshot = future_request
    path, _result = terminal(future_request, monkeypatch)
    proof = json.loads(path.read_text(encoding="utf-8"))
    if change in {"sending", "unknown"}:
        proof["state"] = change
    elif change == "legacy":
        proof.pop("contract")
    elif change == "send_started":
        proof["send_started"] = True
    elif change == "false_integer":
        proof["send_started"] = 0
    elif change == "future_request":
        proof["request_id"] = "different"
    elif change == "config":
        proof["config_hash"] = "0" * 64
    elif change == "body_hash":
        proof["body_sha256"] = "0" * 64
    elif change == "payload_hash":
        proof["request_payload"]["text"] = "Изменённый текст"
    elif change == "attachment":
        proof["request_attachment"] = {"type": "browser-image"}
    elif change == "phase":
        proof["phase"] = "send"
    elif change == "missing_error":
        proof.pop("error")
    elif change == "snapshot_hash":
        proof["snapshot_hash"] = "0" * 64
    elif change == "presentation":
        proof["presentation"] = {"title_format": "bold"}
    elif change == "delivery":
        proof["delivery"] = {"state": "confirmed"}
    elif change == "receipt":
        proof["receipt"] = {"state": "unknown"}
    elif change == "permalink":
        proof["permalink"] = "https://max.ru/1"
    elif change == "payload_send_started":
        proof["request_payload"]["send_started"] = True
        proof["content_hash"] = fingerprint(proof["request_payload"])
    elif change == "timestamp_boolean":
        proof["failed_at"] = True
    elif change == "timestamp_nan":
        proof["failed_at"] = float("nan")
    elif change == "partial_write":
        path.with_suffix(".new").write_text('{"state":', encoding="utf-8")
    elif change == "invalid_json":
        path.write_text('{"state":', encoding="utf-8")
    elif change == "duplicate_json_key":
        path.write_text(path.read_text(encoding="utf-8")[:-1] + ',"send_started":false}', encoding="utf-8")
    if change not in {"invalid_json", "duplicate_json_key"}:
        path.write_text(json.dumps(proof), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AgentError, match="^BROWSER_PRECLICK_PROOF_INVALID$"):
        read_preclick_failure(settings, payload["request_id"], snapshot=snapshot, chat_id="-123")
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("destination", "-456"), ("route", "max_api"),
    ("platform", "pikabu"), ("body", "Другой текст"), ("config_hash", "wrong"),
    ("title", "Другой заголовок"), ("media", [{"mime": "image/png", "path": "different", "sha256": "a" * 64}])])
def test_proof_is_bound_to_the_complete_dispatch_snapshot(future_request, monkeypatch, field, value):
    settings, payload, snapshot = future_request
    terminal(future_request, monkeypatch)
    snapshot = {**snapshot, field: value}
    with pytest.raises(AgentError, match="^BROWSER_PRECLICK_PROOF_INVALID$"):
        read_preclick_failure(settings, payload["request_id"], snapshot=snapshot, chat_id="-123")


def test_missing_proof_read_creates_no_directories(future_request):
    settings, payload, snapshot = future_request
    directory = settings.root / "data/browser-publications"
    assert not directory.exists()
    with pytest.raises(AgentError, match="BROWSER_PRECLICK_PROOF_INVALID"):
        read_preclick_failure(settings, payload["request_id"], snapshot=snapshot)
    assert not directory.exists()


@pytest.mark.parametrize("state", ["sending", "unknown", "confirmed", "preparing"])
def test_existing_journal_never_becomes_a_new_preclick_proof(future_request, monkeypatch, state):
    settings, payload, snapshot = future_request
    path = browser_worker.ledger_path(settings, payload["request_id"])
    journal = {"state": state, "content_hash": fingerprint(payload), "delivery": {"state": "confirmed"}}
    browser_worker.save(path, journal)
    before = path.read_bytes()
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("browser reopened"))
    assert browser_worker.publish(settings, payload)["state"] == ("confirmed" if state == "confirmed" else "unknown")
    assert path.read_bytes() == before
    with pytest.raises(AgentError, match="BROWSER_PRECLICK_PROOF_INVALID"):
        read_preclick_failure(settings, payload["request_id"], snapshot=snapshot)


def test_interrupted_claim_write_never_opens_browser_or_becomes_proof(future_request, monkeypatch):
    settings, payload, snapshot = future_request
    path = browser_worker.ledger_path(settings, payload["request_id"])
    monkeypatch.setattr(browser_worker.os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("disk flush failed")))
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("browser opened after failed flush"))
    assert browser_worker.publish(settings, payload) == {"state": "unknown"}
    assert not path.exists() and path.with_suffix(".new").exists()
    assert browser_worker.publish(settings, payload) == {"state": "unknown"}
    with pytest.raises(AgentError, match="BROWSER_PRECLICK_PROOF_INVALID"):
        read_preclick_failure(settings, payload["request_id"], snapshot=snapshot)


def test_overlapping_request_cannot_claim_failure_for_the_first_process(future_request, monkeypatch):
    settings, payload, snapshot = future_request
    nested = []

    def competing(*_args, **_kwargs):
        nested.append(browser_worker.publish(settings, payload))
        raise AgentError("BROWSER_PROFILE_BUSY")

    monkeypatch.setattr(browser_worker, "_perform_publish", competing)
    result = browser_worker.publish(settings, payload)
    assert nested == [{"state": "unknown"}]
    assert result["state"] == "preclick_failed"
    assert read_preclick_failure(settings, payload["request_id"], snapshot=snapshot)["proof"]["error"]["code"] == "BROWSER_PROFILE_BUSY"


def test_sending_or_lost_owner_can_never_be_rewritten_as_preclick_failure(future_request, monkeypatch):
    settings, payload, snapshot = future_request

    def interrupted(_settings, _payload, path, state, **_kwargs):
        state.update(state="sending", send_started=True)
        browser_worker.save(path, state)
        raise RuntimeError("click outcome unavailable")

    monkeypatch.setattr(browser_worker, "_perform_publish", interrupted)
    assert browser_worker.publish(settings, payload) == {"state": "unknown"}
    path = browser_worker.ledger_path(settings, payload["request_id"])
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "sending"
    with pytest.raises(AgentError, match="BROWSER_PRECLICK_PROOF_INVALID"):
        read_preclick_failure(settings, payload["request_id"], snapshot=snapshot)


def test_exact_composer_wait_is_bounded_and_never_rewrites_text(monkeypatch):
    clock = SimpleNamespace(now=0.0, checks=0)

    def wait(milliseconds):
        clock.now += milliseconds / 1000

    def text():
        clock.checks += 1
        return "Текст ещё не готов"

    composer = SimpleNamespace(evaluate=lambda *_: True, inner_text=text)
    page = SimpleNamespace(wait_for_timeout=wait)
    monkeypatch.setattr(browser_worker.time, "monotonic", lambda: clock.now)
    with pytest.raises(AgentError, match="BROWSER_COMPOSER_TEXT_NOT_READY"):
        browser_worker._wait_composer_text(page, composer, "Точный текст", timeout_seconds=1)
    assert 1 <= clock.now < 1.11 and 9 <= clock.checks <= 11
