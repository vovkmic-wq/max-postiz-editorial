"""The explicit hourly policy has its own limits; other profiles retain theirs."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Draft, uid
from max_postiz_agent.hub_models import Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from test_hourly_human_review import human_approve, queued

pytest_plugins = ["test_automatic_policy"]


def previous_delivery(core, prepared, due, *, actual=None):
    """Persist historical delivery evidence, never run a publisher in this test."""
    with core.db.session.begin() as session:
        candidate = session.get(Variant, prepared["variant_id"])
        current = session.get(Draft, candidate.draft_id)
        draft = Draft(channel_id=current.channel_id, story_id=uid(), current=current.current,
                      config_id=current.config_id)
        session.add(draft)
        session.flush()
        variant = Variant(draft_id=draft.id, target_id=candidate.target_id, title="Историческая статья",
                          body="Другой ранее опубликованный материал.", source_revision=1,
                          config_hash=candidate.config_hash, binding_hash=candidate.binding_hash)
        session.add(variant)
        session.flush()
        approval = TargetApproval(variant_id=variant.id, revision=1, snapshot_hash=uid(),
                                  scheduled_at=due, actor="historical_policy")
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=variant.id, approval_id=approval.id, revision=1,
                            snapshot_hash=uid(), snapshot={}, due_at=due, state="published", verified=True)
        session.add(dispatch)
        session.flush()
        session.add(DispatchAttempt(dispatch_id=dispatch.id, result="published",
                                    created_at=due if actual is None else actual))


def check_limits(core, prepared, due, policy=None):
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        target = session.get(Target, variant.target_id)
        core._check_schedule_limits(session, variant, target, due, automatic_policy=policy)


def test_second_automatic_article_uses_policy_despite_profile_120_minutes(automatic_cycle):
    (cycle, core, article, records, _, stamp, _), _ = automatic_cycle
    publication = core.settings.channel("main")["publication"]
    publication.update(min_gap_minutes=120, max_posts_per_day=16)
    prepared = cycle.prepare(article)
    due = stamp[0] + 120
    previous_delivery(core, prepared, due - 3615, actual=due - 3601)

    planned = cycle.publish(prepared["id"], prepared["content_hash"])

    assert planned["state"] == "pending" and records[prepared["postiz_id"]]["state"] == "QUEUE"
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.due_at == due and dispatch.state == "pending" and dispatch.attempts == 0
        assert session.get(TargetApproval, dispatch.approval_id).actor == "hourly_article_policy"
    assert publication["min_gap_minutes"] == 120 and publication["max_posts_per_day"] == 16


def test_no_policy_override_retains_generic_profile_120_minute_limit(automatic_cycle):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    core.settings.channel("main")["publication"]["min_gap_minutes"] = 120
    prepared = cycle.prepare(article)
    due = stamp[0] + 120
    previous_delivery(core, prepared, due - 3601)
    with pytest.raises(AgentError, match="SCHEDULE_GAP_TOO_SHORT"):
        check_limits(core, prepared, due)


def test_human_hourly_approval_keeps_generic_profile_limits(human_cycle):
    cycle, core, article, _, _, stamp, _ = human_cycle
    core.settings.channel("main")["publication"]["min_gap_minutes"] = 120
    prepared = cycle.prepare(article)
    due = stamp[0] + 180
    previous_delivery(core, prepared, due - 3601)
    with pytest.raises(AgentError, match="SCHEDULE_GAP_TOO_SHORT"):
        human_approve(human_cycle, prepared)
    with core.db.session() as session:
        assert not session.scalar(select(TargetApproval).where(TargetApproval.variant_id == prepared["variant_id"]))


def test_automatic_daily_24_limit_is_distinct_from_profile_16(automatic_cycle):
    (cycle, core, article, _, _, _, policy), _ = automatic_cycle
    core.settings.channel("main")["publication"].update(min_gap_minutes=120, max_posts_per_day=16)
    prepared = cycle.prepare(article)
    # 00:00 Moscow, then a candidate at 23:30 of the same policy day.
    day = datetime(2026, 10, 19, 21, tzinfo=UTC).timestamp()
    due = day + 23.5 * 3600
    for hour in range(23):
        previous_delivery(core, prepared, day + hour * 3600)
    check_limits(core, prepared, due, policy)
    with pytest.raises(AgentError, match="DAILY_LIMIT_REACHED"):
        check_limits(core, prepared, due)
    previous_delivery(core, prepared, day + 1800)
    with pytest.raises(AgentError, match="DAILY_LIMIT_REACHED"):
        check_limits(core, prepared, due, policy)


def test_automatic_schedule_gap_under_3600_still_blocks(automatic_cycle):
    (cycle, core, article, _, _, stamp, policy), _ = automatic_cycle
    prepared = cycle.prepare(article)
    due = stamp[0] + 120
    previous_delivery(core, prepared, due - 3599)
    with pytest.raises(AgentError, match="SCHEDULE_GAP_TOO_SHORT"):
        check_limits(core, prepared, due, policy)


def test_automatic_actual_delivery_gap_is_retained_in_final_approval(automatic_cycle):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    due = stamp[0] + 180
    # Scheduled gap is safe; the real prior delivery was delayed.
    previous_delivery(core, prepared, due - 4000, actual=due - 3599)
    revision, hashed, due = queued(automatic_cycle[0], prepared)
    with pytest.raises(AgentError, match="ARTICLE_HOURLY_GAP"):
        core.approve_hourly_policy(prepared["variant_id"], revision, hashed, due)
    with core.db.session() as session:
        assert not session.scalar(select(TargetApproval).where(TargetApproval.variant_id == prepared["variant_id"]))


@pytest.mark.parametrize("change", [
    {"min_gap_seconds": 3599}, {"min_gap_seconds": True}, {"max_per_day": 25},
    {"max_per_day": True}, {"target_id": "another"}, {"approval_mode": "human"},
])
def test_invalid_override_cannot_weaken_schedule_limits(automatic_cycle, change):
    (cycle, core, article, _, _, stamp, policy), _ = automatic_cycle
    prepared = cycle.prepare(article)
    with pytest.raises(AgentError, match="ARTICLE_POLICY_INVALID"):
        check_limits(core, prepared, stamp[0] + 120, {**policy, **change})


@pytest.mark.parametrize("history", ["revoked_approval", "cancelled_dispatch", "approval_audit"])
def test_final_automatic_approval_rejects_any_prior_history(automatic_cycle, history):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    revision, hashed, due = queued(automatic_cycle[0], prepared)
    if history == "cancelled_dispatch":
        # A legacy/imported dispatch may reference an older approval. The
        # current variant still has dispatch history even without its own row.
        previous_delivery(core, prepared, stamp[0] - 8000)
    with core.db.session.begin() as session:
        if history == "revoked_approval":
            session.add(TargetApproval(variant_id=prepared["variant_id"], revision=revision,
                snapshot_hash="0" * 64, scheduled_at=due, actor="editor", revoked=True))
        elif history == "cancelled_dispatch":
            old_approval = session.scalar(select(TargetApproval))
            session.add(Dispatch(variant_id=prepared["variant_id"], approval_id=old_approval.id,
                revision=revision, snapshot_hash="0" * 64, snapshot={}, due_at=due, state="cancelled"))
        else:
            session.add(Audit(actor="editor", action="target_approve", entity=prepared["variant_id"]))
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        core.approve_hourly_policy(prepared["variant_id"], revision, hashed, due)
    with core.db.session() as session:
        assert not session.scalar(select(Dispatch).where(
            Dispatch.variant_id == prepared["variant_id"], Dispatch.state == "pending"))
        assert session.get(Target, "max").paused
