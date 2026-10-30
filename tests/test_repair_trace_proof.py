"""Strict saved refusal proof: no editor/model/remote call during status or payload."""
import copy
import json

import pytest

from max_postiz_agent.article_repair_cycle import AutomaticArticleRepair
from max_postiz_agent.config import AgentError

pytest_plugins = ["test_automatic_policy"]


@pytest.fixture
def refusal(automatic_cycle):
    (cycle, core, article, remote, writes, stamp, _), review = automatic_cycle
    review.update(approved=False, issues=["A scientific fact was omitted."])
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    record = copy.deepcopy(cycle._load()["records"][0])
    path = core.settings.root / record["quality_trace"]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {"cycle": cycle, "core": core, "article": article, "record": record,
            "path": path, "events": events, "raw": path.read_bytes(), "remote": remote,
            "writes": writes, "stamp": stamp, "review": review,
            "proof": AutomaticArticleRepair(cycle)}


def save(case):
    case["path"].write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in case["events"]) + "\n",
                            encoding="utf-8")


def forbid_models(case, monkeypatch):
    monkeypatch.setattr(case["cycle"], "_quality", lambda *_a, **_k: pytest.fail("proof called model factory"))


def test_complete_negative_review_proof_is_read_only_and_current_status_is_eligible(refusal, monkeypatch):
    case = refusal
    forbid_models(case, monkeypatch)
    result = case["proof"]._payload(case["record"])
    assert result["article"] == case["article"]
    assert result["candidate"] == next(e["translation"] for e in case["events"] if e["phase"] == "translation_parsed")
    assert any(e["phase"] == "review_returned" for e in result["feedback"]["events"])
    assert case["proof"].status(case["record"])["eligible"] is True
    case["stamp"][0] += 3600
    assert case["proof"].status(case["record"]) == {"eligible": False, "reason": "EXPIRED", "attempts": 0}
    assert case["path"].read_bytes() == case["raw"] and not case["remote"] and not case["writes"]


def test_legacy_internal_repeats_are_bound_to_the_first_real_refusal(refusal, monkeypatch):
    case = refusal
    ledger = case["cycle"]._load()
    ledger["records"] = []
    case["cycle"]._save(ledger)
    quality = case["cycle"]._quality

    def mutable(*args, **kwargs):
        value = quality(*args, **kwargs)
        value.translator.immutable_candidates = False
        return value

    monkeypatch.setattr(case["cycle"], "_quality", mutable)
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        case["cycle"].prepare(case["article"])
    current = case["cycle"]._load()["records"][0]
    forbid_models(case, monkeypatch)
    result = case["proof"]._payload(current)
    assert result["candidate"] == next(e["translation"] for e in case["events"] if e["phase"] == "translation_parsed")
    assert len([e for e in result["feedback"]["events"] if e["phase"] == "review_returned"]) == 1
    assert len([e for e in result["feedback"]["events"] if e["phase"] == "retry"]) == 3


def test_changed_candidate_latest_segment_is_selected_even_at_identical_timestamps(refusal, monkeypatch):
    case = refusal
    old = next(e["translation"] for e in case["events"] if e["phase"] == "translation_parsed")
    candidate = {**old, "body": old["body"] + " Исследователи также описали свойства звёздного ветра."}
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        case["cycle"].prepare(case["article"], retry_record=case["record"]["id"], candidate=candidate)
    current = case["cycle"]._load()["records"][0]
    raw = case["path"].read_bytes()
    forbid_models(case, monkeypatch)
    result = case["proof"]._payload(current)
    assert result["candidate"] == candidate
    assert len(current["preparation_attempts"]) == 2 and raw.startswith(case["raw"])
    assert case["path"].read_bytes() == raw and not case["remote"]


def test_real_gate_refusal_is_recomputed_without_review_or_spelling(refusal, monkeypatch):
    case = refusal
    # Another independent fresh fixture record supplies an authentic emitted gate failure.
    cycle = case["cycle"]
    ledger = cycle._load()
    ledger["records"] = []
    cycle._save(ledger)
    article = {**case["article"], "original": case["article"]["original"] + " Scientists used 2 measurements."}
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    current = cycle._load()["records"][0]
    forbid_models(case, monkeypatch)
    result = case["proof"]._payload(current)
    assert result["article"] == article
    phases = [e["phase"] for e in result["feedback"]["events"]]
    assert phases == ["gate_rejected", "retry", "rejected"]


def test_positive_review_with_real_fresh_lt_refusal_is_proven(refusal, monkeypatch):
    case = refusal
    ledger = case["cycle"]._load()
    ledger["records"] = []
    case["cycle"]._save(ledger)
    case["review"].update(approved=True, issues=[])
    quality = case["cycle"]._quality
    match = {"message": "Unknown word", "offset": 0, "length": 5,
             "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling", "category": {"id": "TYPOS"}}}

    def spelling(*args, **kwargs):
        value = quality(*args, **kwargs)
        value._spelling_matches = lambda _: [copy.deepcopy(match)]
        return value

    monkeypatch.setattr(case["cycle"], "_quality", spelling)
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        case["cycle"].prepare(case["article"])
    current = case["cycle"]._load()["records"][0]
    forbid_models(case, monkeypatch)
    result = case["proof"]._payload(current)
    returned = next(e for e in result["feedback"]["events"] if e["phase"] == "spelling_returned")
    assert returned["matches"] == [match]


@pytest.mark.parametrize("damage", [
    "duplicate_json", "nonfinite_time", "boolean_time", "reverse_time", "boolean_attempt", "unfinished",
    "terminal_feedback", "raw_review", "parsed_review", "review_source", "review_prompt", "review_hash",
    "review_rubric", "lt_unresolved", "lt_matches", "missing_phase", "fake_positive", "source", "input_hash",
    "gate_characters", "extra_call", "extra_event", "blank_line", "outside_window", "attempt_number",
])
def test_damaged_trace_never_allows_current_repair_or_expired_autoclose(refusal, monkeypatch, damage):
    case = refusal
    events, record = case["events"], case["record"]
    first = events[0]
    if damage == "nonfinite_time":
        events[-1]["at"] = float("inf")
    elif damage == "boolean_time":
        first["at"] = True
    elif damage == "reverse_time":
        events[-1]["at"] = first["at"] - 1
    elif damage == "boolean_attempt":
        events[-1]["attempt"] = True
    elif damage == "unfinished":
        record["preparation_attempts"][-1].pop("finished_at")
    elif damage == "outside_window":
        record["preparation_attempts"][-1]["started_at"] += 1
    elif damage == "attempt_number":
        record["preparation_attempts"][-1]["number"] = True
    elif damage == "terminal_feedback":
        events[-1]["feedback"] = {}
    elif damage in {"raw_review", "parsed_review", "fake_positive"}:
        returned = next(e for e in events if e["phase"] == "review_returned")
        parsed = next(e for e in events if e["phase"] == "review_parsed")
        if damage == "raw_review":
            returned["raw"] = '{"approved":true,"topic":"science","issues":[],"warnings":[]}'
        elif damage == "parsed_review":
            parsed["review"]["issues"] = ["Different refusal"]
        else:
            review = {"approved": True, "topic": "science", "issues": [], "warnings": []}
            returned["raw"] = json.dumps(review)
            parsed["review"] = review
            expected = {"review_approved": True, "topic": "science", "review_issues": [], "spelling_matches": []}
            next(e for e in events if e["phase"] == "retry")["feedback"] = expected
            events[-1]["feedback"] = expected
    elif damage.startswith("review_"):
        context = next(e["context"] for e in events if e["phase"] == "review_started")
        key = {"review_source": "source_untrusted", "review_prompt": "instruction",
               "review_hash": "review_prompt_hash", "review_rubric": "review_rubric"}[damage]
        context[key] = "changed"
    elif damage == "lt_unresolved":
        next(e for e in events if e["phase"] == "spelling_adjudicated")["unresolved"] = [{"forged": True}]
    elif damage == "lt_matches":
        next(e for e in events if e["phase"] == "spelling_returned")["matches"] = None
    elif damage == "missing_phase":
        events.remove(next(e for e in events if e["phase"] == "spelling_started"))
    elif damage == "source":
        first["context"]["source_untrusted"]["body"] += " Another source."
    elif damage == "input_hash":
        record["input_hash"] = "f" * 64
    elif damage == "gate_characters":
        next(e for e in events if e["phase"] == "gate_passed")["characters"] += 1
    elif damage == "extra_call":
        events.extend(copy.deepcopy(events))
    elif damage == "extra_event":
        events.append({**events[-1], "phase": "spelling_failed", "error": "ARTICLE_SPELLING_UNAVAILABLE"})
    save(case)
    if damage == "duplicate_json":
        raw = case["path"].read_text(encoding="utf-8")
        case["path"].write_text(raw.replace('"phase": "translation_started"',
            '"phase": "translation_started", "phase": "translation_started"', 1), encoding="utf-8")
    elif damage == "blank_line":
        case["path"].write_bytes(case["path"].read_bytes() + b"\n")
    raw = case["path"].read_bytes()
    forbid_models(case, monkeypatch)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID"):
        case["proof"]._payload(record)
    assert case["proof"].status(record) == {"eligible": False, "reason": "TRACE_INVALID", "attempts": 0}
    case["stamp"][0] += 3600
    assert case["proof"].status(record)["reason"] == "TRACE_INVALID"
    assert case["path"].read_bytes() == raw and not case["remote"] and not case["writes"]
