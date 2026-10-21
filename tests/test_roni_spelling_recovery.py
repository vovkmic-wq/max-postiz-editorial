"""RONI recovery is an independent, two-hit, exact saved-trace contract."""
import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select

from max_postiz_agent.automatic_correction import recover_staged_roni_spelling, recover_staged_spelling
from max_postiz_agent.automatic_policy import automatic_evidence
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Material
from max_postiz_agent.hub_models import BridgePost, Dispatch, TargetApproval, Variant
from test_automatic_correction import (REASON, correction_case as base_correction_case,
                                      no_delivery, run)

pytest_plugins = ["test_automatic_correction"]

RONI = {"title": "График относительного океанического индекса Ниньо (RONI)",
        "body": "Диаграмма показывает относительный океанический индекс Ниньо (RONI), который используется "
                "учёными для наблюдения за изменениями температуры океана и состоянием климата."}


def hits(text):
    import re
    return [{"offset": len(text[:m.start()].encode("utf-16-le")) // 2, "length": len("Ниньо"),
             "type": {"typeName": "UnknownWord"},
             "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}}
            for m in re.finditer("Ниньо", text)]


@pytest.fixture
def roni_case(automatic_cycle, monkeypatch):
    (_, core, article, _, _, _, policy), _ = automatic_cycle
    policy["allowed_hosts"] = ["svs.gsfc.nasa.gov"]
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy), encoding="utf-8")
    article.update(url="https://svs.gsfc.nasa.gov/5685/", title="Chart of Relative Oceanic Niño Index (RONI)",
                   original="This chart shows the Relative Oceanic Niño Index (RONI). Scientists use the chart "
                            "to observe changing sea surface temperatures and understand the ocean and climate.")
    case = base_correction_case.__wrapped__(automatic_cycle, monkeypatch, SimpleNamespace(param=None))
    case["matches"] = hits(RONI["title"] + "\n\n" + RONI["body"])
    from max_postiz_agent.article_quality import rubric_spelling
    with monkeypatch.context() as old_release:
        old_release.setattr("max_postiz_agent.article_quality.rubric_spelling",
                            lambda *args, **kwargs: ([], copy.deepcopy(args[2])) if args[2] else rubric_spelling(*args, **kwargs))
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            run(case, RONI)
    saved = case["cycle"]._load()["records"][0]
    case["failed_record"] = copy.deepcopy(saved)
    case["failed_attempt"] = copy.deepcopy(saved["automatic_corrections"][0])
    case["failed_path"] = case["core"].settings.root / case["failed_attempt"]["quality_trace"]
    case["failed_bytes"] = case["failed_path"].read_bytes()
    case["trace_sha"] = hashlib.sha256(case["failed_bytes"]).hexdigest()
    case["pending_hash"] = fingerprint(case["cycle"]._snapshot(saved))
    case["review_calls"].clear()
    case["spelling_calls"].clear()
    return case


def recover(case, **changes):
    args = {"cycle": case["cycle"], "ident": case["prepared"]["id"], "correction": RONI,
            "reviewed_hash": case["pending_hash"], "trace_sha256": case["trace_sha"], "reason": REASON}
    return recover_staged_roni_spelling(**{**args, **changes})


def not_checked(case):
    assert not case["review_calls"] and not case["spelling_calls"] and not case["updates"]
    with case["core"].db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    assert len(case["writes"]) == len(case["records"]) == 1


def test_two_hit_recovery_preserves_claims_raw_and_same_draft_without_model(roni_case):
    case = roni_case
    result = case["cycle"].recover_staged_roni_spelling(case["prepared"]["id"], RONI,
                                                      case["pending_hash"], case["trace_sha"], REASON)
    assert result["state"] == "automatic_ready" and result["postiz_id"] == case["prepared"]["postiz_id"]
    assert not case["review_calls"] and len(case["spelling_calls"]) == len(case["updates"]) == 1
    saved = case["cycle"]._load()["records"][0]
    assert saved["automatic_corrections"] == case["failed_record"]["automatic_corrections"]
    assert not saved.get("automatic_spelling_recoveries")
    attempt = saved["automatic_roni_spelling_recoveries"][0]
    assert attempt["state"] == "completed" and attempt["kind"] == "source_bound_roni_spelling"
    assert attempt["no_model_call"] is True
    assert case["failed_path"].read_bytes() == case["failed_bytes"]
    assert case["old_trace"].read_bytes() == case["old_bytes"]
    with case["core"].db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        evidence = variant.metadata_json["machine_review_evidence"]
        assert len(evidence["spelling_adjudicated"]) == 2 and evidence["spelling_unresolved"] == []
        failed_raw = next(json.loads(x)["raw"] for x in case["failed_bytes"].splitlines()
                          if json.loads(x).get("phase") == "review_returned")
        assert evidence["review_raw"] == failed_raw and automatic_evidence(session, variant)
        assert variant.metadata_json["quality"] == "saved_ministral_fresh_gates_languagetool_roni_recovery"
        assert session.scalar(select(Audit).where(Audit.action == "hourly_staged_roni_spelling_recovery_started"))
        assert not session.scalar(select(Audit).where(Audit.action == "hourly_staged_spelling_recovery_started"))
        assert variant.metadata_json["automatic_roni_spelling_recovery_claim"] == attempt["claim_id"]
    no_delivery(case)


def test_original_mur_command_cannot_recover_roni(roni_case):
    case = roni_case
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_TRACE_INVALID"):
        recover_staged_spelling(case["cycle"], case["prepared"]["id"], RONI,
                                case["pending_hash"], case["trace_sha"], REASON)
    assert not case["cycle"]._load()["records"][0].get("automatic_spelling_recoveries")
    not_checked(case)


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
def test_roni_command_cannot_recover_mur(spelling_case):
    from test_automatic_correction import SCIENTIFIC_CORRECTION
    case = spelling_case
    with pytest.raises(AgentError, match="ARTICLE_RONI_SPELLING_RECOVERY_TRACE_INVALID"):
        recover(case, correction=SCIENTIFIC_CORRECTION)
    not_checked(case)


@pytest.mark.parametrize("change", ["hash", "trace_hash", "candidate", "slot", "unknown", "mutation", "source", "unpaused", "lease"])
def test_roni_guards_fail_before_fresh_check(roni_case, change):
    case = roni_case
    kwargs = {}
    if change == "hash":
        kwargs["reviewed_hash"] = "0" * 64
    elif change == "trace_hash":
        kwargs["trace_sha256"] = "0" * 64
    elif change == "candidate":
        kwargs["correction"] = {**RONI, "body": RONI["body"] + " Изменённая версия."}
    elif change == "slot":
        case["stamp"][0] += 3600
    elif change in {"unknown", "mutation"}:
        ledger = case["cycle"]._load()
        failure = ledger["records"][0]["automatic_corrections"][0]
        if change == "unknown":
            failure["error"] = "POSTIZ_UNKNOWN"
        else:
            failure["mutation_started_at"] = case["stamp"][0]
        case["cycle"]._save(ledger)
    else:
        with case["core"].db.session.begin() as session:
            if change == "source":
                session.scalar(select(Material)).text += " Changed source."
            elif change == "unpaused":
                from max_postiz_agent.hub_models import Target
                session.get(Target, "max").paused = False
            else:
                session.get(BridgePost, case["prepared"]["postiz_id"]).mutation_owner = "another-editor"
    with pytest.raises(AgentError):
        recover(case, **kwargs)
    not_checked(case)


@pytest.mark.parametrize("change", ["negative", "issues", "positions", "duplicate", "one_hit", "non_lt", "other_lt", "source_url", "source_text", "prompt"])
def test_roni_saved_trace_must_be_exact_positive_and_exact_two_distinct_hits(roni_case, change):
    case = roni_case
    events = [json.loads(x) for x in case["failed_bytes"].splitlines()]
    if change in {"negative", "issues"}:
        review = copy.deepcopy(events[4]["review"])
        if change == "negative":
            review["approved"] = False
        else:
            review["issues"] = ["A substantive discrepancy."]
        events[3]["raw"] = json.dumps(review)
        events[4]["review"] = review
    elif change in {"source_url", "source_text"}:
        events[0]["source_untrusted"]["url" if change == "source_url" else "body"] += "changed"
    elif change == "prompt":
        events[2]["context"]["review_prompt_hash"] = "0" * 64
    else:
        matches = copy.deepcopy(events[6]["matches"])
        if change == "positions":
            matches[0]["offset"] += 1
        elif change == "duplicate":
            matches = [matches[0], matches[0]]
        elif change == "one_hit":
            matches = matches[:1]
        elif change == "non_lt":
            matches[0]["rule"]["id"] = "REAL_GRAMMAR"
        else:
            matches.append({"rule": {"id": "OTHER_LT", "issueType": "grammar"}})
        events[6]["matches"] = matches
        events[7]["unresolved"] = matches
        events[8]["feedback"]["spelling_matches"] = matches
    case["failed_path"].write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    changed_hash = hashlib.sha256(case["failed_path"].read_bytes()).hexdigest()
    with pytest.raises(AgentError, match="ARTICLE_RONI_SPELLING_RECOVERY_TRACE_INVALID"):
        recover(case, trace_sha256=changed_hash)
    not_checked(case)


def test_repeat_or_deleted_ledger_claim_cannot_replay(roni_case):
    case = roni_case
    case["matches"] = [{"rule": {"id": "ACTUAL_GRAMMAR", "issueType": "grammar"}}]
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        recover(case)
    assert not case["review_calls"] and not case["updates"] and len(case["spelling_calls"]) == 1
    ledger = case["cycle"]._load()
    original = copy.deepcopy(ledger["records"][0]["automatic_corrections"])
    ledger["records"][0].pop("automatic_roni_spelling_recoveries")
    case["cycle"]._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED"):
        recover(case)
    assert case["cycle"]._load()["records"][0]["automatic_corrections"] == original
    with case["core"].db.session() as session:
        claim = str(uuid5(NAMESPACE_URL, "max-postiz:staged-roni-spelling-recovery:" + case["prepared"]["id"]))
        assert session.get(Audit, claim)
    no_delivery(case)


def test_unknown_native_post_keeps_claim_and_lease_without_retry(roni_case):
    case = roni_case

    def lost_response():
        raise AgentError("POSTIZ_UNKNOWN", 503)

    case["after_mutation"] = lost_response
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        recover(case)
    assert len(case["updates"]) == len(case["spelling_calls"]) == 1 and not case["review_calls"]
    with case["core"].db.session() as session:
        assert session.get(BridgePost, case["prepared"]["postiz_id"]).mutation_owner
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED"):
        recover(case)
    assert len(case["updates"]) == 1


def test_roni_db_claim_survives_ledger_write_crash_and_cannot_switch_to_mur(roni_case, monkeypatch):
    case = roni_case
    with monkeypatch.context() as crash:
        crash.setattr(case["cycle"], "_save", lambda _: (_ for _ in ()).throw(OSError("private ledger unavailable")))
        with pytest.raises(OSError):
            recover(case)
    saved = case["cycle"]._load()["records"][0]
    assert not saved.get("automatic_roni_spelling_recoveries")
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED"):
        recover(case)
    current = fingerprint(case["cycle"]._snapshot(saved))
    with pytest.raises(AgentError):
        recover_staged_spelling(case["cycle"], saved["id"], RONI, current, case["trace_sha"], REASON)
    not_checked(case)


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
def test_mur_db_claim_survives_ledger_crash_and_cannot_switch_to_roni(spelling_case, monkeypatch):
    from test_automatic_correction import SCIENTIFIC_CORRECTION
    case = spelling_case
    with monkeypatch.context() as crash:
        crash.setattr(case["cycle"], "_save", lambda _: (_ for _ in ()).throw(OSError("private ledger unavailable")))
        with pytest.raises(OSError):
            recover_staged_spelling(case["cycle"], case["prepared"]["id"], SCIENTIFIC_CORRECTION,
                                    case["pending_hash"], case["trace_sha"], REASON)
    saved = case["cycle"]._load()["records"][0]
    current = fingerprint(case["cycle"]._snapshot(saved))
    with pytest.raises(AgentError):
        recover(case, correction=SCIENTIFIC_CORRECTION, reviewed_hash=current)
    not_checked(case)
