"""Полный переход исправленного текста через штатный worker; внешние сервисы — fake."""

import copy

import pytest
from sqlalchemy import select

from max_postiz_agent.automatic_policy import POLICY_ACTOR
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft, Material
from max_postiz_agent.hub_models import Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from test_automatic_article_repair import SECOND_BODY, current, patched

pytest_plugins = ["test_automatic_article_repair"]


@pytest.mark.parametrize("first_correction_rejected", [False, True])
def test_repaired_article_is_scheduled_delivered_once_and_paused(
    repair_cycle, bridge, first_correction_rejected
):
    value = repair_cycle
    sent = bridge[3]
    value.core.publisher.mode = "browser"

    # Повторная оценка допустима только для следующего изменённого кандидата.
    if first_correction_rejected:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            value.cycle.repair_rejected(value.initial["id"])
        assert current(value)["automatic_repair_attempts"][0]["state"] == "rejected"
        assert not value.records and not sent
        value.controls.output = lambda context: patched(context, SECOND_BODY)
    value.review.update(approved=True, issues=[])
    prepared = value.cycle.repair_rejected(value.initial["id"])
    repaired = copy.deepcopy(current(value))
    claims = repaired["automatic_repair_attempts"]
    expected_claims = ["rejected", "passed"] if first_correction_rejected else ["passed"]
    expected_checks = len(expected_claims) + 1
    counts = {"hy": 1, "min": expected_checks, "lt": expected_checks, "qwen": len(expected_claims)}

    assert [claim["state"] for claim in claims] == expected_claims
    assert repaired["preparation_attempts"][0] == value.initial["preparation_attempts"][0]
    assert repaired["slot"] == value.initial["slot"]
    assert repaired["input_hash"] == value.initial["input_hash"]
    assert repaired["original_hash"] == value.initial["original_hash"]
    assert repaired["url"] == value.initial["url"]
    assert value.trace.read_bytes().startswith(value.initial_trace)
    assert value.counts == counts
    assert len(value.records) == len(value.writes) == 1
    post_id = prepared["postiz_id"]
    assert value.records[post_id]["state"] == "DRAFT"
    with value.core.db.session() as session:
        assert session.query(Draft).count() == session.query(Variant).count() == 1
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert all(target.paused for target in session.scalars(select(Target)))
        variant = session.get(Variant, prepared["variant_id"])
        assert variant.metadata_json["automatic_repair_claim"] == claims[-1]["id"]
        exact_body = variant.body

    # QUEUE меняет revision/hash, но не создаёт новый Postiz post и не меняет текст.
    planned = value.cycle.publish(prepared["id"], prepared["content_hash"])
    queued = current(value)
    assert planned["state"] == "pending" and planned["postiz_id"] == post_id
    assert value.records[post_id]["state"] == "QUEUE"
    assert value.records[post_id]["content"] == exact_body
    assert queued["prepared_hash"] != repaired["prepared_hash"]
    assert queued["automatic_repair_attempts"] == claims
    assert len(value.records) == len(value.writes) == 1 and value.counts == counts
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        approval = session.get(TargetApproval, dispatch.approval_id)
        assert approval.actor == POLICY_ACTOR
        assert dispatch.snapshot["body"] == exact_body
        assert dispatch.snapshot_hash == fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 1
        assert not session.get(Target, "max").paused
        assert all(target.paused for target in session.scalars(select(Target)) if target.id != "max")
        assert session.scalar(select(Audit).where(Audit.action == "hourly_article_policy_review"))
        assert not session.scalar(select(Audit).where(Audit.action == "hourly_article_human_review"))

    assert value.core.dispatch_tick() == 0 and not sent
    value.stamp[0] += 121
    assert value.core.dispatch_tick() == 1
    assert value.core.dispatch_tick() == 0
    assert len(sent) == 1
    assert sent[0][0][0] == "-123" and sent[0][0][1] == exact_body
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.state == "published" and dispatch.verified and dispatch.attempts == 1
        assert dispatch.snapshot["body"] == exact_body
        attempts = list(session.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id)))
        assert len(attempts) == 1 and attempts[0].result == "published"
        assert all(target.paused for target in session.scalars(select(Target)))

    # Ответ Postiz подтверждается отдельно, как в живой сверке после доставки.
    value.records[post_id]["state"] = "PUBLISHED"
    verified = value.cycle.status(prepared["id"])
    assert verified["state"] == "published" and verified["delivery_verified"] is True
    assert verified["postiz_state"] == "PUBLISHED"
    assert value.records[post_id]["content"] == exact_body
    assert current(value)["automatic_repair_attempts"] == claims
    assert value.trace.read_bytes().startswith(value.initial_trace)

    # Повторный publish и повтор исходника не создают ни отправку, ни новый перевод.
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        value.cycle.publish(prepared["id"], prepared["content_hash"])
    value.stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare(value.article)
    assert value.counts == counts and len(sent) == len(value.records) == len(value.writes) == 1
    with value.core.db.session() as session:
        assert session.query(Material).count() == session.query(Variant).count() == 1
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 1
        assert all(target.paused for target in session.scalars(select(Target)))
