"""Durable-исправление проходит настоящие QA/БД/DRAFT переходы без сети и моделей."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.article_repair import (ArticleRepair, REPAIR_CONTRACT, REPAIR_MODEL,
                                             repair_prompt_name)
from max_postiz_agent.automatic_policy import automatic_evidence
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Draft
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.server_cycle import ServerCycle
from max_postiz_agent.server_state import ServerState

pytest_plugins = ["test_automatic_policy"]

CORRECT_BODY = "Учёные изучили необычную атмосферу далёкой звезды и обнаружили звёздный ветер."
SECOND_BODY = "Изучая необычную атмосферу далёкой звезды, учёные обнаружили звёздный ветер."


@pytest.fixture
def repair_cycle(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    cycle, core, article, records, writes, stamp, policy = values
    prompt = Path(__file__).resolve().parents[1] / "prompts/ARTICLE_REPAIR.txt"
    (core.settings.root / "prompts/ARTICLE_REPAIR.txt").write_bytes(prompt.read_bytes())
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    counts = {"hy": 0, "min": 0, "lt": 0, "qwen": 0}
    review_prompts, editor_prompts = [], []
    original_quality = cycle._quality
    controls = SimpleNamespace(output=None, editor_hook=None, reviewer_hook=None, factories=0)

    def quality(*args, **kwargs):
        result = original_quality(*args, **kwargs)
        for model, key in ((result.translator, "hy"), (result.reviewer, "min")):
            original = model.generate_text

            def counted(prompt, *, original=original, key=key):
                counts[key] += 1
                if key == "min":
                    review_prompts.append(json.loads(prompt))
                    if controls.reviewer_hook:
                        controls.reviewer_hook()
                return original(prompt)

            model.generate_text = counted
        spelling = result._spelling_matches

        def checked(text):
            counts["lt"] += 1
            return spelling(text)

        result._spelling_matches = checked
        return result

    monkeypatch.setattr(cycle, "_quality", quality)
    review.update(approved=False, issues=["The translation omits the discovery of the stellar wind."])
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    initial = copy.deepcopy(cycle._load()["records"][0])
    trace = core.settings.root / initial["quality_trace"]
    initial_trace = trace.read_bytes()
    assert counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 0}
    assert not records and not writes and not initial.get("variant_id")
    stamp[0] += 1

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            counts["qwen"] += 1
            context = json.loads(prompt.split("UNTRUSTED_JSON_DATA:\n", 1)[1])
            editor_prompts.append(context)
            if controls.editor_hook:
                controls.editor_hook()
            output = controls.output(context) if callable(controls.output) else controls.output
            if isinstance(output, BaseException):
                raise output
            if output is None:
                output = patched(context, CORRECT_BODY)
            return (output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)), {}

    def factory(trace):
        controls.factories += 1
        return ArticleRepair(Editor(), prompt_dir=core.settings.root / "prompts", trace=trace)

    cycle.repair_factory = factory
    return SimpleNamespace(cycle=cycle, core=core, article=article, records=records, writes=writes,
        stamp=stamp, policy=policy, review=review, counts=counts, controls=controls,
        initial=initial, trace=trace, initial_trace=initial_trace, review_prompts=review_prompts,
        editor_prompts=editor_prompts)


def patched(context, body):
    previous = context["candidate_untrusted"]
    return {"candidate": {**previous, "body": body}, "changes": [
        {"field": "body", "before": previous["body"], "after": body,
         "source_quote": "Scientists studied an unusual atmosphere and discovered a stellar wind in the distant star.",
         "reason": "Восстановлен факт об обнаружении звёздного ветра без изменения исходных научных данных."}]}


def current(value):
    return value.cycle._load()["records"][0]


def no_delivery(value, *, no_draft=True, paused=True):
    assert not value.records and not value.writes
    with value.core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        if no_draft:
            assert session.query(Draft).count() == session.query(Variant).count() == 0
        assert session.get(Target, "max").paused is paused


def claim_audits(value):
    with value.core.db.session() as session:
        return list(session.scalars(select(Audit).where(Audit.action == "automatic_article_repair_claim")))


def test_repair_success_uses_fresh_independent_qa_and_only_one_draft_without_approval(repair_cycle):
    value = repair_cycle
    assert value.cycle.automatic_repair_status(current(value)) == {
        "eligible": True, "reason": "READY", "attempts": 0}
    value.review.update(approved=True, issues=[])
    prepared = value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert prepared["state"] == saved["state"] == "automatic_ready"
    assert prepared["id"] == value.initial["id"] and prepared["url"] == value.article["url"]
    assert value.counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
    assert len(value.records) == len(value.writes) == 1
    assert value.records[prepared["postiz_id"]]["state"] == "DRAFT"
    assert value.writes[0][3:] == ("draft", None)
    assert value.trace.read_bytes().startswith(value.initial_trace)
    assert saved["preparation_attempts"][0] == value.initial["preparation_attempts"][0]
    claim = saved["automatic_repair_attempts"][0]
    assert claim["state"] == "passed" and claim["number"] == 1
    assert claim_audits(value)[0].id == claim["id"]
    assert (value.core.settings.root / claim["snapshot_path"]).is_file()
    assert (value.core.settings.root / claim["repair_trace"]).is_file()
    assert value.review_prompts[-1]["source_untrusted"] == value.article["title"].strip() + "\n\n" + value.article["original"].strip()
    assert value.review_prompts[-1]["translation_untrusted"].endswith(CORRECT_BODY)
    with value.core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert automatic_evidence(session, variant) is not None
        assert variant.metadata_json["quality"] == "automatic_correction_ministral_languagetool"
        assert variant.metadata_json["automatic_repair_claim"] == claim["id"]
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert session.get(Target, "max").paused
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NOT_ELIGIBLE|ARTICLE_AUTOMATIC_REPAIR_EXHAUSTED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts["qwen"] == 1 and len(value.records) == 1


@pytest.mark.parametrize("output", ["not json", {"candidate": {}, "approved": True}])
def test_complete_invalid_editor_response_is_quarantined_without_second_qa_or_model_retry(repair_cycle, output):
    value = repair_cycle
    value.controls.output = output
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_RESPONSE_INVALID|ARTICLE_REPAIR_PATCH_INVALID"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert saved["automatic_repair_attempts"][0]["state"] == "not_repairable"
    assert saved["automatic_repair_attempts"][0]["outcome_origin"] == "protocol_guard"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NOT_REPAIRABLE"):
        value.cycle.repair_rejected(value.initial["id"])
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "EXPIRED"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_EXPIRED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    raw = (value.core.settings.root / saved["automatic_repair_attempts"][0]["repair_trace"]).read_text(encoding="utf-8")
    assert "repair_returned" in raw and "repair_failed" in raw
    assert value.trace.read_bytes() == value.initial_trace
    no_delivery(value)


def test_whitespace_only_proposal_stops_before_fresh_reviewer_without_resampling(repair_cycle):
    value = repair_cycle
    value.controls.output = lambda context: patched(context, context["candidate_untrusted"]["body"].replace(" ", "  ", 1))
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_UNCHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert current(value)["automatic_repair_attempts"][0]["state"] == "no_change"
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "NO_CHANGE"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NO_CHANGE"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


def test_explicit_not_repairable_preserves_refusal_and_never_calls_fresh_qa(repair_cycle):
    value = repair_cycle
    value.controls.output = {"repairable": False,
        "reason": "Верный научный термин нельзя заменять только для подавления орфографического словаря."}
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert current(value)["automatic_repair_attempts"][0]["state"] == "not_repairable"
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "NOT_REPAIRABLE"
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert value.trace.read_bytes() == value.initial_trace
    no_delivery(value)


def test_two_distinct_corrections_with_fresh_negative_reviews_exhaust_budget_across_restart(repair_cycle):
    value = repair_cycle
    bodies = iter([CORRECT_BODY, SECOND_BODY])
    value.controls.output = lambda context: patched(context, next(bodies))
    for number in (1, 2):
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            value.cycle.repair_rejected(value.initial["id"])
        saved = current(value)
        assert len(saved["automatic_repair_attempts"]) == number
        assert saved["automatic_repair_attempts"][-1]["state"] == "rejected"
        assert value.counts == {"hy": 1, "min": number + 1, "lt": number + 1, "qwen": number}
        value.stamp[0] += 1
    restarted = HourlyArticleCycle(value.core, repair_factory=lambda _trace: pytest.fail("Исчерпанный claim не вызывает Qwen"))
    assert restarted.automatic_repair_status(current(value))["reason"] == "EXHAUSTED"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_EXHAUSTED"):
        restarted.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 3, "lt": 3, "qwen": 2}
    assert len(claim_audits(value)) == 2 and value.trace.read_bytes().startswith(value.initial_trace)
    no_delivery(value)


def test_audit_commits_before_failed_ledger_save_and_blocks_replay_even_after_expiry(repair_cycle, monkeypatch):
    value = repair_cycle
    original_save = value.cycle._save

    def crash(ledger):
        if ledger["records"][0].get("automatic_repair_attempts"):
            raise OSError("simulated lost ledger write")
        return original_save(ledger)

    monkeypatch.setattr(value.cycle, "_save", crash)
    with pytest.raises(OSError, match="lost ledger"):
        value.cycle.repair_rejected(value.initial["id"])
    assert len(claim_audits(value)) == 1 and not current(value).get("automatic_repair_attempts")
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "CLAIM_LOST"
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "CLAIM_LOST"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST"):
        value.cycle.repair_rejected(value.initial["id"])
    controller = ServerCycle(value.core, cycle=value.cycle, clock=lambda: value.stamp[0])
    assert controller._close_known_rejection(current(value)) is False
    assert value.controls.factories == value.counts["qwen"] == 0
    no_delivery(value)


@pytest.mark.parametrize("damage", ["missing_audit", "missing_ledger_claim", "audit_binding"])
def test_audit_ledger_mismatch_is_not_hidden_by_expiry(repair_cycle, damage):
    value = repair_cycle
    value.controls.output = "not json"
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_RESPONSE_INVALID"):
        value.cycle.repair_rejected(value.initial["id"])
    claim = current(value)["automatic_repair_attempts"][0]
    if damage == "missing_ledger_claim":
        ledger = value.cycle._load()
        ledger["records"][0].pop("automatic_repair_attempts")
        value.cycle._save(ledger)
    else:
        with value.core.db.session.begin() as session:
            audit = session.get(Audit, claim["id"])
            if damage == "missing_audit":
                session.delete(audit)
            else:
                audit.details = {**audit.details, "input_hash": "changed"}
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "CLAIM_LOST"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


def test_interrupted_editor_claim_remains_in_progress_without_second_request(repair_cycle):
    value = repair_cycle
    value.controls.output = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "IN_PROGRESS"
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "IN_PROGRESS"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_IN_PROGRESS"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


def test_paused_before_repair_does_not_reserve_claim_or_call_editor(repair_cycle):
    value = repair_cycle
    ServerState(value.core).initialize()
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert not claim_audits(value) and value.controls.factories == 0
    assert current(value) == value.initial
    no_delivery(value)


def add_unrelated_lease(value, *, owner=True):
    with value.core.db.session.begin() as session:
        target = session.get(Target, "max")
        draft = Draft(channel_id=target.profile_id, story_id="unrelated", current={}, config_id=value.core.settings.hash)
        session.add(draft)
        session.flush()
        variant = Variant(draft_id=draft.id, target_id=target.id, body="Другой материал.",
            source_revision=1, config_hash=value.core.settings.hash, binding_hash=target.binding_hash)
        session.add(variant)
        session.flush()
        session.add(BridgePost(post_id="other-draft", integration_id="integration-max", group_id="other",
            target_id=target.id, variant_id=variant.id, remote_hash="a" * 64, remote_state="DRAFT",
            due_at=value.stamp[0], mutation_owner="other-owner" if owner else None,
            mutation_until=value.stamp[0] + 60))


@pytest.mark.parametrize("owner", [False, True])
def test_existing_owner_or_orphan_until_lease_blocks_before_editor(repair_cycle, owner):
    value = repair_cycle
    add_unrelated_lease(value, owner=owner)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NOT_QUIET"):
        value.cycle.repair_rejected(value.initial["id"])
    assert not claim_audits(value) and value.controls.factories == 0
    no_delivery(value, no_draft=False)


@pytest.mark.parametrize("damage", ["ledger_input", "input_file", "trace", "repair_prompt", "review_prompt", "pause", "hour"])
def test_changed_inputs_during_editor_are_not_overwritten_or_sent_to_fresh_qa(repair_cycle, damage):
    value = repair_cycle

    def tamper():
        if damage == "ledger_input":
            ledger = value.cycle._load()
            ledger["records"][0]["input_hash"] = "different-source"
            value.cycle._save(ledger)
        elif damage == "input_file":
            path = value.core.settings.root / value.initial["input_file"]
            path.write_bytes(path.read_bytes() + b" ")
        elif damage == "trace":
            value.trace.write_bytes(value.trace.read_bytes() + b"\n")
        elif damage in {"repair_prompt", "review_prompt"}:
            name = repair_prompt_name(REPAIR_CONTRACT) if damage == "repair_prompt" else "ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
            path = value.core.settings.root / "prompts" / name
            path.write_bytes(path.read_bytes() + b"\nChanged after claim.\n")
        elif damage == "pause":
            ServerState(value.core).initialize()
        else:
            value.stamp[0] = (value.initial["slot"] + 1) * 3600

    value.controls.editor_hook = tamper
    value.review.update(approved=True, issues=[])
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED|EDITORIAL_PAUSED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    if damage == "ledger_input":
        assert current(value)["input_hash"] == "different-source"
    assert not value.cycle.automatic_repair_status(current(value))["eligible"]
    no_delivery(value)


def test_review_prompt_changed_during_fresh_qa_blocks_before_any_draft(repair_cycle):
    value = repair_cycle
    value.review.update(approved=True, issues=[])

    def changed():
        path = value.core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
        path.write_bytes(path.read_bytes() + b"\nChanged during QA.\n")

    value.controls.reviewer_hook = changed
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("reference", ["snapshot_path", "proposal_path"])
def test_snapshot_or_exact_proposal_changed_during_ministral_qa_cannot_create_draft(repair_cycle, reference):
    value = repair_cycle
    value.review.update(approved=True, issues=[])

    def changed():
        claim = current(value)["automatic_repair_attempts"][-1]
        path = value.core.settings.root / claim[reference]
        path.write_bytes(path.read_bytes() + b" ")

    value.controls.reviewer_hook = changed
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    assert value.counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
    assert not value.cycle.automatic_repair_status(current(value))["eligible"]
    no_delivery(value)


def test_crash_after_saved_draft_before_claim_finalization_never_schedules_on_restart(repair_cycle, monkeypatch):
    value = repair_cycle
    value.review.update(approved=True, issues=[])
    original_save = value.cycle._save

    def crash_after_ready(ledger):
        original_save(ledger)
        record = ledger["records"][0]
        attempts = record.get("automatic_repair_attempts", [])
        if record["state"] == "automatic_ready" and attempts and attempts[-1]["state"] == "checking":
            raise KeyboardInterrupt()

    monkeypatch.setattr(value.cycle, "_save", crash_after_ready)
    with pytest.raises(KeyboardInterrupt):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert saved["state"] == "automatic_ready"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "IN_PROGRESS"
    assert len(value.records) == 1 and value.records[saved["postiz_id"]]["state"] == "DRAFT"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_IN_PROGRESS"):
        value.cycle.publish(saved["id"], value.cycle._view(saved)["content_hash"])
    store = ServerState(value.core)
    store.initialize()
    store.control("start", 1, "owner")
    controller = ServerCycle(value.core, state=store, cycle=value.cycle, clock=lambda: value.stamp[0])
    try:
        result = controller.run_once()
    except AgentError as exc:
        assert exc.code in {"ARTICLE_AUTOMATIC_REPAIR_IN_PROGRESS", "ARTICLE_AUTOMATIC_NOT_READY"}
    else:
        assert result["phase"] == "blocked"
    assert len(value.records) == len(value.writes) == 1
    assert value.records[saved["postiz_id"]]["state"] == "DRAFT"
    with value.core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    assert value.counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
