"""Postiz millisecond GET must preserve the exact signed native dispatch date."""
import copy
from datetime import UTC, datetime

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Draft, uid
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from max_postiz_agent.server_models import PublicationSlot

pytest_plugins = ["test_hourly_reserve_controller"]


def rows(core, model):
    with core.db.session() as session:
        return [{column.name: copy.deepcopy(getattr(row, column.name))
                 for column in model.__table__.columns} for row in session.query(model).all()]


@pytest.fixture
def millisecond_pending(reserve_controller, monkeypatch):
    value = reserve_controller
    value.stamp[0] = int(value.stamp[0]) + .701083
    request = value.core.postiz.request

    def millisecond_postiz(*args, **kwargs):
        result = request(*args, **kwargs)
        for remote in value.posts.values():
            remote["publishDate"] = datetime.fromisoformat(
                remote["publishDate"]).isoformat(timespec="milliseconds")
        return result

    monkeypatch.setattr(value.core.postiz, "request", millisecond_postiz)
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    remote = value.posts[record["postiz_id"]]
    assert abs(value.core.bridge.normalize(remote)[2] - record["due_at"]) > 0
    value.state.control("pause", value.state.read()["revision"], "test-owner")
    control = value.state.read()
    value.state.settings(control["settings"], control["revision"], "test-owner")
    value.state.control("start", value.state.read()["revision"], "test-owner")
    gets = []

    def get(ident):
        gets.append(ident)
        return copy.deepcopy(value.posts[ident])

    monkeypatch.setattr(value.core.postiz, "get_post", get)
    monkeypatch.setattr(value.core.postiz, "request", lambda *_a, **_k: pytest.fail("POST on resume"))
    monkeypatch.setattr(value.core.postiz, "create", lambda *_a, **_k: pytest.fail("create on resume"))
    return value, record, remote, gets


def resume(value, record):
    return value.controller._resume_pending(record, record["dispatch_id"],
        value.state.read()["settings"], value.cycle._policy_for_record(record))


def test_real_millisecond_get_refences_only_existing_signed_pending(millisecond_pending, monkeypatch):
    value, record, remote, gets = millisecond_pending
    before = {model: rows(value.core, model) for model in
              (Dispatch, TargetApproval, BridgePost, Variant, PublicationSlot)}
    ledger = value.cycle._load()
    original_remote = copy.deepcopy(remote)
    cadence_dates = []
    cadence = value.cycle._cadence

    def exact_cadence(policy, due, *args, **kwargs):
        cadence_dates.append(due)
        return cadence(policy, due, *args, **kwargs)

    monkeypatch.setattr(value.cycle, "_cadence", exact_cadence)
    assert resume(value, record) is True
    assert gets == [record["postiz_id"]]
    assert cadence_dates == [record["due_at"]]
    assert value.cycle._load() == ledger and remote == original_remote
    for model in (Dispatch, TargetApproval, BridgePost, Variant):
        assert rows(value.core, model) == before[model]
    after = rows(value.core, PublicationSlot)[0]
    old = before[PublicationSlot][0]
    assert after["control_revision"] == value.state.read()["revision"] > old["control_revision"]
    assert {k: v for k, v in after.items() if k not in {"control_revision", "updated_at"}} == {
        k: v for k, v in old.items() if k not in {"control_revision", "updated_at"}}
    with value.core.db.session() as session:
        assert session.get(Target, "max").paused is False
    assert len(value.writes) == 1


@pytest.mark.parametrize("damage", ["body", "revision", "lease", "unknown", "date", "missing_date", "bad_date"])
def test_millisecond_tolerance_preserves_pending_guards(millisecond_pending, damage):
    value, record, remote, gets = millisecond_pending
    with value.core.db.session.begin() as session:
        dispatch = session.get(Dispatch, record["dispatch_id"])
        if damage == "revision":
            dispatch.revision += 1
        elif damage == "lease":
            session.get(BridgePost, record["postiz_id"]).mutation_owner = "other"
        elif damage == "unknown":
            dispatch.state = "unknown"
    if damage == "body":
        remote["content"] += " Изменённый текст."
    elif damage == "date":
        remote["publishDate"] = datetime.fromtimestamp(record["due_at"] + .002, UTC).isoformat()
    elif damage == "missing_date":
        del remote["publishDate"]
    elif damage == "bad_date":
        remote["publishDate"] = "invalid"
    before = {model: rows(value.core, model) for model in (Dispatch, TargetApproval, PublicationSlot)}
    with pytest.raises(AgentError):
        resume(value, record)
    assert gets == [record["postiz_id"]]
    for model in before:
        assert rows(value.core, model) == before[model]
    with value.core.db.session() as session:
        assert session.get(Target, "max").paused is True
    assert len(value.writes) == 1


@pytest.mark.parametrize("offset", [-.0009, .0009, -.002, .002, float("nan"), float("inf")])
def test_observed_precision_tolerance_is_finite_and_bounded(millisecond_pending, monkeypatch, offset):
    value, record, remote, _ = millisecond_pending
    body, media, _, hashed = value.core.bridge.normalize(remote)
    monkeypatch.setattr(value.core.bridge, "normalize", lambda _: (
        body, media, record["due_at"] + offset, hashed))
    if abs(offset) < .001:
        assert resume(value, record) is True
    else:
        with pytest.raises(AgentError, match="EDITORIAL_PENDING_RESUME_NOT_SAFE"):
            resume(value, record)


@pytest.mark.parametrize("gap", [3601, 3600.5])
def test_actual_delivery_gap_uses_exact_signed_date(millisecond_pending, gap):
    value, record, _, _ = millisecond_pending
    with value.core.db.session.begin() as session:
        current = session.get(Variant, record["variant_id"])
        draft = session.get(Draft, current.draft_id)
        draft_data = {c.name: copy.deepcopy(getattr(draft, c.name)) for c in Draft.__table__.columns}
        draft_data.update(id=uid(), story_id=uid())
        previous_draft = Draft(**draft_data)
        session.add(previous_draft)
        session.flush()
        variant_data = {c.name: copy.deepcopy(getattr(current, c.name)) for c in Variant.__table__.columns}
        variant_data.update(id=uid(), draft_id=previous_draft.id, state="published")
        previous = Variant(**variant_data)
        session.add(previous)
        session.flush()
        approval = TargetApproval(variant_id=previous.id, revision=previous.revision,
            snapshot_hash="1" * 64, scheduled_at=record["due_at"] - gap,
            actor="hourly_article_policy", revoked=False)
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=previous.id, approval_id=approval.id,
            revision=previous.revision, snapshot_hash="1" * 64, snapshot={},
            state="published", due_at=approval.scheduled_at, attempts=1, verified=True)
        session.add(dispatch)
        session.flush()
        session.add(DispatchAttempt(dispatch_id=dispatch.id, result="published",
            created_at=record["due_at"] - gap))
    if gap == 3601:
        assert resume(value, record) is True
    else:
        with pytest.raises(AgentError, match="ARTICLE_HOURLY_GAP"):
            resume(value, record)
