"""Observation identity does not imply a sender lifecycle."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def readback(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("readback_observation_test", scripts / "readback_max.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_report_separates_unknown_sender_from_publication(readback):
    report = readback.new_report("record-1")
    report["state"] = "verified"
    assert report["state"] == "verified"
    assert report["delivery_sender_attribution"] == {
        "status": "unknown", "reason": "SENDER_LIFECYCLE_EVIDENCE_UNAVAILABLE"
    }
    assert report["upload_or_send_calls"] == 0


def test_reconciled_observation_keeps_one_physical_send_and_two_observations(tmp_path, monkeypatch, readback):
    monkeypatch.setattr(readback, "ROOT", tmp_path)
    record_id = "81e4db37-a169-42e0-825e-0bc9252c2a24"
    dispatch_id = "52125814-e3ff-486b-890a-a9d636389c03"
    dispatch = SimpleNamespace(id=dispatch_id, attempts=1, snapshot_hash="snapshot",
                               snapshot={"body": "body", "media": [{"sha256": "media"}]})
    unknown = SimpleNamespace(id="unknown-observation", dispatch_id=dispatch_id, result="unknown",
                              error="UNKNOWN", created_at=1)
    published = SimpleNamespace(id="published-observation", dispatch_id=dispatch_id, result="published",
                                error=None, created_at=2)
    journal = {"state": "confirmed", "request_id": dispatch_id, "text": "body",
               "delivery": {"state": "confirmed", "external_id": "browser-" + dispatch_id,
                            "image_proof": {"uploaded_sha256": "media"}}}
    path = tmp_path / "data/browser-publications" / (dispatch_id + ".json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(journal), encoding="utf-8")
    keys = ("id", "dispatch_id", "result", "error", "created_at")
    audit = SimpleNamespace(actor="article_reserve", action="existing_max_message_reconciled", entity=dispatch_id,
        details={"contract": "existing-max-image-observation-no-send-v1", "physical_send_count": 1,
                 "new_publish_calls": 0,
                 "original_unknown_attempt": {key: getattr(unknown, key) for key in keys},
                 "published_observation": {key: getattr(published, key) for key in keys},
                 "proposal": {"record_id": record_id, "dispatch_id": dispatch_id, "snapshot_hash": "snapshot"},
                 "journal_before_sha256": "c07364331f263cc530571caace17a5e0ac041df0201d256f99a798244da0841b",
                 "journal_after_sha256": readback.sha(path.read_bytes())})
    db = SimpleNamespace(get=lambda _model, _id: audit)
    assert readback.published_observation(db, dispatch, record_id, [unknown, published]).id == "published-observation"
    dispatch.attempts = 2
    with pytest.raises(ValueError, match="ONE_PHYSICAL_SEND_REQUIRED"):
        readback.published_observation(db, dispatch, record_id, [unknown, published])


def test_stdout_keeps_unknown_sender_even_when_content_verified(tmp_path, monkeypatch, capsys, readback):
    monkeypatch.setattr(readback, "ROOT", tmp_path)
    output = tmp_path / "private-report.json"
    output.write_text("{}", encoding="utf-8")
    report = readback.new_report("record-1")
    report["state"] = "verified"
    readback.print_summary(report, output, None)
    message = json.loads(capsys.readouterr().out)
    assert message["state"] == "verified"
    assert message["delivery_sender_attribution"] == {
        "status": "unknown", "reason": "SENDER_LIFECYCLE_EVIDENCE_UNAVAILABLE"
    }
    assert message["external_mutations_performed"] == 0
