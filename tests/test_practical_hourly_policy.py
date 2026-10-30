"""Практическая рубрика имеет собственные exact evidence, без переоценки истории."""
import copy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import automatic_review_rubric, review_rubric_binding
from max_postiz_agent.article_quality import ArticleQuality, HOURLY_PRACTICAL_REVIEW_RUBRIC
from max_postiz_agent.automatic_policy import automatic_evidence
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Draft
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant

pytest_plugins = ["test_automatic_policy"]
RUBRIC = HOURLY_PRACTICAL_REVIEW_RUBRIC


@pytest.fixture
def practical_cycle(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    cycle, core, _, _, _, _, policy = values
    policy["review_rubric"] = RUBRIC
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    prompt = Path(__file__).resolve().parents[1] / "prompts/ARTICLE_REVIEW_HOURLY_PRACTICAL.txt"
    (core.settings.root / "prompts" / prompt.name).write_bytes(prompt.read_bytes())
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    from test_automatic_policy import Model
    candidate = {"title": "Космический ветер",
        "body": "Учёные изучили необычные процессы в атмосфере далёкой звезды."}
    full = candidate["title"] + "\n\n" + candidate["body"]
    match = {"offset": full.index("далёкой"), "length": len("далёкой"),
        "message": "Синтетическое словарное замечание",
        "type": {"typeName": "UnknownWord"},
        "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling",
                 "category": {"id": "TYPOS"}}}

    def quality(record_id=None, review_only=False):
        return ArticleQuality(Model(copy.deepcopy(candidate), "hy-mt2-7b-q8"),
            Model(copy.deepcopy(review), "ministral"), prompt_dir=core.settings.root / "prompts",
            trace=cycle._trace_writer(record_id), review_rubric=RUBRIC,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": [match]})))
    monkeypatch.setattr(cycle, "_quality", quality)
    return values, review, match


def test_practical_preparation_retains_raw_advice_and_binds_record_attempt_audit(practical_cycle):
    (cycle, core, article, records, calls, _, _), _, match = practical_cycle
    prepared = cycle.prepare(article)
    record = cycle._load()["records"][0]
    assert record["review_rubric"] == record["preparation_attempts"][0]["review_rubric"] == RUBRIC
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert review_rubric_binding(core, session, record, variant) == RUBRIC
        evidence = variant.metadata_json["machine_review_evidence"]
        assert evidence["spelling_matches"] == [match]
        assert evidence["spelling_unresolved"] == []
        assert evidence["spelling_adjudicated"][0]["reason"] == "NON_BLOCKING_PRACTICAL_LANGUAGE_ADVICE"
        assert automatic_evidence(session, variant) is not None
        audit = session.scalar(select(Audit).where(Audit.entity == record["id"],
            Audit.action == "hourly_article_prepare_attempt"))
        assert audit.details["review_rubric"] == RUBRIC
    assert len(records) == len(calls) == 1


def test_practical_full_fake_chain_stages_and_approves_exact_hourly_dispatch(practical_cycle):
    from max_postiz_agent.automatic_policy import POLICY_ACTOR
    (cycle, core, article, records, calls, stamp, _), _, _ = practical_cycle
    core.publisher.mode = "browser"
    prepared = cycle.prepare(article)
    assert prepared["state"] == "automatic_ready" and records[prepared["postiz_id"]]["state"] == "DRAFT"
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    assert planned["state"] == "pending" and records[planned["postiz_id"]]["state"] == "QUEUE"
    assert len(calls) == 1 and calls[0][3] == "draft"
    with core.db.session() as session:
        approval = session.query(TargetApproval).one()
        dispatch = session.query(Dispatch).one()
        audit = session.scalar(select(Audit).where(Audit.entity == approval.id,
            Audit.action == "hourly_article_policy_review"))
        assert approval.actor == audit.actor == POLICY_ACTOR
        assert audit.details["review_rubric"] == RUBRIC
        assert dispatch.state == "pending"
    stamp[0] += 121
    assert core.dispatch_tick() == 1
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.state == "published" and dispatch.verified and dispatch.attempts == 1
        assert session.get(Target, "max").paused
    records[planned["postiz_id"]]["state"] = "PUBLISHED"
    assert cycle.status(prepared["id"])["state"] == "published"


@pytest.mark.parametrize("field", ["record", "attempt", "audit"])
def test_practical_marker_removal_does_not_turn_record_into_old_rubric(practical_cycle, field):
    (cycle, core, article, _, _, _, _), _, _ = practical_cycle
    prepared = cycle.prepare(article)
    record = cycle._load()["records"][0]
    if field == "record":
        record.pop("review_rubric")
    elif field == "attempt":
        record["preparation_attempts"][0].pop("review_rubric")
    else:
        with core.db.session.begin() as session:
            audit = session.scalar(select(Audit).where(Audit.entity == record["id"],
                Audit.action == "hourly_article_prepare_attempt"))
            audit.details = {k: v for k, v in audit.details.items() if k != "review_rubric"}
    with core.db.session() as session:
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_RUBRIC_BINDING_INVALID"):
            review_rubric_binding(core, session, record, session.get(Variant, prepared["variant_id"]))


def test_practical_semantic_rejection_still_creates_no_draft(practical_cycle):
    (cycle, core, article, records, calls, _, _), review, _ = practical_cycle
    review.update(approved=False, issues=["The main object is changed to a constellation."])
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    assert not records and not calls
    record = cycle._load()["records"][0]
    assert record["review_rubric"] == RUBRIC and record["state"] == "needs_attention"
    with core.db.session() as session:
        assert session.query(Draft).count() == 0
        assert review_rubric_binding(core, session, record) == RUBRIC


def test_unmarked_old_record_keeps_meaning_first_after_new_policy_is_selected(practical_cycle):
    (cycle, _, _, _, _, _, policy), _, _ = practical_cycle
    assert policy["review_rubric"] == RUBRIC
    assert cycle._policy()["review_rubric"] == RUBRIC
    assert automatic_review_rubric({"id": "historical"}) == "meaning-first-v1"
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_RUBRIC_INVALID"):
        automatic_review_rubric({"review_rubric": "unknown-future-rubric"})


def test_practical_repair_replays_own_rejection_prompt_and_preserves_claim_limit(practical_cycle):
    from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
    from max_postiz_agent.article_repair_cycle import AutomaticArticleRepair, MAX_AUTOMATIC_REPAIRS
    (cycle, core, article, _, _, _, _), review, _ = practical_cycle
    review.update(approved=False, issues=["The discovery of a stellar wind is missing."])
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    record = cycle._load()["records"][0]
    payload = AutomaticArticleRepair(cycle)._payload(record)
    assert payload["review_rubric"] == RUBRIC
    prompts = AutomaticArticleRepair(cycle)._prompts(rubric=RUBRIC)
    assert "ARTICLE_REVIEW_HOURLY_PRACTICAL.txt" in prompts
    assert "ARTICLE_REVIEW_MEANING_FIRST_V1.txt" not in prompts
    assert MAX_AUTOMATIC_REPAIRS == 2
    initial_trace = (core.settings.root / record["quality_trace"]).read_bytes()
    editor_calls = []

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            editor_calls.append(prompt)
            return json.dumps({"title": "Космический ветер", "body":
                "Учёные изучили необычную атмосферу далёкой звезды и обнаружили звёздный ветер."},
                ensure_ascii=False), {}

    cycle.repair_factory = lambda trace: ArticleRepair(Editor(), prompt_dir=core.settings.root / "prompts", trace=trace)
    review.update(approved=True, issues=[])
    result = cycle.repair_rejected(record["id"])
    assert result["state"] == "automatic_ready" and len(editor_calls) == 1
    updated = cycle._load()["records"][0]
    claim = updated["automatic_repair_attempts"][0]
    assert claim["state"] == "passed" and claim["review_rubric"] == RUBRIC
    assert (core.settings.root / record["quality_trace"]).read_bytes().startswith(initial_trace)
    with core.db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert review_rubric_binding(core, session, updated, variant) == RUBRIC
        assert automatic_evidence(session, variant) is not None
    with pytest.raises(AgentError):
        cycle.repair_rejected(record["id"])
    assert len(editor_calls) == 1
