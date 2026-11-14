"""Adding a source must preserve already checked, unassigned reserve and its history."""

import copy

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, ConfigVersion, DraftRevision
from max_postiz_agent.hub_models import Target, TargetApproval, VariantRevision
from max_postiz_agent.source_activation import migrate_ready

pytest_plugins = ["test_hourly_reserve_controller"]


def align_runtime(value):
    with value.core.db.session() as session:
        for item in value.core.publishing["targets"]:
            target = session.get(Target, item["id"])
            for name in ("profile_id", "platform", "route", "destination", "enabled", "options"):
                item[name] = copy.deepcopy(getattr(target, name))
    value.core.sync_targets()


def candidate(value):
    settings = copy.deepcopy(value.core.settings)
    source = copy.deepcopy(settings.source("example_rss"))
    source.update(
        id="eso_potw",
        enabled=True,
        type="html",
        endpoint="https://www.eso.org/public/images/potw/",
        extraction_profile="server_science",
    )
    source["rights"].update(
        verified=True,
        basis="cc_by_4_0_text",
        allow_storage=True,
        allow_derivative=True,
        allow_full_repost=True,
        evidence_ref="https://www.eso.org/public/outreach/copyright/",
    )
    settings.sources["sources"].append(source)
    settings.sources["revision"] += 1
    return settings


def exercise_ready_rebind(v):
    align_runtime(v)
    v.controller.prepare_once()
    old = copy.deepcopy(v.cycle._load()["records"][0])
    models = list(v.model_calls)
    claims_before = database_state(v.core)["editorial_preparation_claims"]
    result = migrate_ready(
        v.cycle,
        candidate(v),
        expected_config_hash=v.core.settings.hash,
        expected_control_revision=v.state.read()["revision"],
    )
    assert result["added_sources"] == ["eso_potw"]
    assert result["rebound_records"] == [old["id"]]
    assert result["approvals_created"] == result["external_calls"] == 0
    fresh = v.cycle._load()["records"][0]
    assert fresh["prepared_hash"] != old["prepared_hash"]
    assert {
        k: x for k, x in fresh.items() if k not in {"prepared_hash", "reserve_prepared_hash"}
    } == {k: x for k, x in old.items() if k not in {"prepared_hash", "reserve_prepared_hash"}}
    v.cycle.validate_reserve(old["id"])
    assert v.model_calls == models and not v.writes
    assert database_state(v.core)["editorial_preparation_claims"] == claims_before
    with v.core.db.session() as s:
        configuration = s.get(ConfigVersion, v.core.settings.hash)
        assert configuration is not None
        assert configuration.snapshot == {
            "topic": v.core.settings.topic,
            "sources": v.core.settings.sources,
        }
        assert len(list(s.scalars(select(VariantRevision)))) == 2
        assert len(list(s.scalars(select(DraftRevision)))) == 2
        assert not list(s.scalars(select(TargetApproval)))
        assert s.scalar(select(Audit).where(Audit.action == "source_configuration_added"))
    v.controller.run_once()
    scheduled = v.cycle._load()["records"][0]
    assert scheduled["state"] == "pending" and len(v.writes) == 1
    v.core.publisher.mode = "browser"
    v.stamp[0] = scheduled["due_at"] + 1
    assert v.core.dispatch_tick() == 1
    assert v.core.dispatch_tick() == 0
    assert v.model_calls == models


def test_pending_activation_is_refused_without_rewriting_approval(reserve_controller):
    v = reserve_controller
    align_runtime(v)
    v.controller.prepare_once()
    v.controller.run_once()
    before = copy.deepcopy(v.cycle._load())
    old_hash = v.core.settings.hash
    with pytest.raises(AgentError, match="SOURCE_ACTIVATION_NOT_SAFE"):
        migrate_ready(
            v.cycle,
            candidate(v),
            expected_config_hash=old_hash,
            expected_control_revision=v.state.read()["revision"],
        )
    assert v.cycle._load() == before and v.core.settings.hash == old_hash
    assert len(v.writes) == 1


def test_ready_reserve_rebind_keeps_evidence_history_and_one_future_delivery(reserve_controller):
    exercise_ready_rebind(reserve_controller)


def test_postgres_ready_source_rebind_preserves_history(postgres_reserve):
    exercise_ready_rebind(postgres_reserve)


@pytest.mark.parametrize(
    "change", ["existing_source", "topic", "publisher", "revision", "disabled", "rights"]
)
def test_non_additive_configuration_is_refused(reserve_controller, change):
    v = reserve_controller
    align_runtime(v)
    v.controller.prepare_once()
    before = copy.deepcopy(v.cycle._load())
    settings = candidate(v)
    if change == "existing_source":
        settings.sources["sources"][0]["name"] += " changed"
    elif change == "topic":
        settings.topic["revision"] += 1
    elif change == "publisher":
        settings.app["max"]["adapter_mode"] = "stub"
    elif change == "revision":
        settings.sources["revision"] += 1
    elif change == "disabled":
        settings.sources["sources"][-1]["enabled"] = False
    else:
        settings.sources["sources"][-1]["rights"]["allow_derivative"] = False
    old_hash = v.core.settings.hash
    with pytest.raises(AgentError):
        migrate_ready(
            v.cycle,
            settings,
            expected_config_hash=old_hash,
            expected_control_revision=v.state.read()["revision"],
        )
    assert v.cycle._load() == before and v.core.settings.hash == old_hash
    assert not v.writes


@pytest.mark.parametrize("guard", ["config_hash", "control_revision", "active_preparation"])
def test_stale_plan_and_active_preparation_are_refused(reserve_controller, guard):
    v = reserve_controller
    align_runtime(v)
    v.controller.prepare_once()
    before = copy.deepcopy(v.cycle._load())
    old_hash = v.core.settings.hash
    revision = v.state.read()["revision"]
    if guard == "active_preparation":
        v.store.begin_preparation(expected_revision=revision)
    with pytest.raises(AgentError, match="SOURCE_ACTIVATION_NOT_SAFE"):
        migrate_ready(
            v.cycle,
            candidate(v),
            expected_config_hash="0" * 64 if guard == "config_hash" else old_hash,
            expected_control_revision=revision + (guard == "control_revision"),
        )
    assert v.cycle._load() == before and v.core.settings.hash == old_hash
    assert not v.writes


def database_state(core):
    from max_postiz_agent.config import fingerprint
    from max_postiz_agent.db import Base

    with core.db.engine.connect() as connection:
        return {
            table.name: sorted(
                fingerprint(dict(row)) for row in connection.execute(table.select()).mappings()
            )
            for table in Base.metadata.sorted_tables
        }


def test_failure_after_rebinding_rolls_back_every_database_change(reserve_controller, monkeypatch):
    v = reserve_controller
    align_runtime(v)
    v.controller.prepare_once()
    before = database_state(v.core)
    old_hash = v.core.settings.hash
    native = v.core._quality

    def fail_new_configuration(*args, **kwargs):
        if v.core.settings.hash != old_hash:
            raise AgentError("ACTIVATION_TEST_FAILURE")
        return native(*args, **kwargs)

    monkeypatch.setattr(v.core, "_quality", fail_new_configuration)
    with pytest.raises(AgentError, match="ACTIVATION_TEST_FAILURE"):
        migrate_ready(
            v.cycle,
            candidate(v),
            expected_config_hash=old_hash,
            expected_control_revision=v.state.read()["revision"],
        )
    assert database_state(v.core) == before and v.core.settings.hash == old_hash
    assert not v.writes


def test_postgres_running_worker_prevents_source_migration(postgres_reserve):
    from sqlalchemy import text
    from max_postiz_agent.server_state import WORKER_LOCK

    v = postgres_reserve
    align_runtime(v)
    v.controller.prepare_once()
    before = database_state(v.core)
    with v.core.db.engine.connect() as owner:
        owner.execute(text("SELECT pg_advisory_lock(:key)"), {"key": WORKER_LOCK})
        owner.commit()
        try:
            with pytest.raises(AgentError, match="SOURCE_ACTIVATION_NOT_SAFE"):
                migrate_ready(
                    v.cycle,
                    candidate(v),
                    expected_config_hash=v.core.settings.hash,
                    expected_control_revision=v.state.read()["revision"],
                )
        finally:
            owner.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
            owner.commit()
    assert database_state(v.core) == before and not v.writes
