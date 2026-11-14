"""Явная миграция только неназначенного резерва при добавлении источников.

Вызывается установщиком при остановленных writers. Файлы конфигурации,
службы и внешние платформы эта операция не изменяет. Pending/unknown запрещены.
"""

import copy

from sqlalchemy import select, text

from .article_sources import registered_policies
from .automatic_policy import automatic_evidence
from .config import AgentError, fingerprint, validate_schema
from .db import Draft, DraftRevision, now
from .hub_models import BridgePost, Dispatch, Target, TargetApproval
from .mobile_store import MobileOperation
from .server_cycle import TERMINAL
from .server_models import (
    EditorialControl,
    EditorialLedger,
    EditorialPreparationClaim,
    PublicationSlot,
)
from .server_state import WORKER_LOCK, fence_editorial_configuration
from .source_adapters import REGISTRY


ERROR = "SOURCE_ACTIVATION_NOT_SAFE"


def require(value):
    if not value:
        raise AgentError(ERROR, 409)


def validate_addition(previous, candidate, policy):
    require(
        previous.root == candidate.root
        and previous.app == candidate.app
        and previous.topic == candidate.topic
    )
    old, new = copy.deepcopy(previous.sources), copy.deepcopy(candidate.sources)
    old_rows, new_rows = old.pop("sources"), new.pop("sources")
    old_revision, new_revision = old.pop("revision"), new.pop("revision")
    require(
        old == new
        and type(old_revision) is int
        and new_revision == old_revision + 1
        and len(new_rows) > len(old_rows)
        and new_rows[: len(old_rows)] == old_rows
    )
    additions = new_rows[len(old_rows) :]
    ids = [row["id"] for row in new_rows]
    require(
        len(ids) == len(set(ids))
        and all(row["id"] in REGISTRY and row["enabled"] is True for row in additions)
    )
    validate_schema(candidate.root, "sources.schema.json", candidate.sources)
    before = {p["source_id"]: fingerprint(p) for p in registered_policies(policy, previous)}
    after = {p["source_id"]: fingerprint(p) for p in registered_policies(policy, candidate)}
    require(
        all(after.get(key) == value for key, value in before.items())
        and set(after) - set(before) == {row["id"] for row in additions}
    )
    return sorted(set(after) - set(before))


def migrate_ready(cycle, candidate, *, expected_config_hash, expected_control_revision):
    """Сохранить старые revisions/QA/claims; новые approvals здесь не создаются."""
    core, previous = cycle.core, cycle.core.settings
    require(
        previous.hash == expected_config_hash
        and cycle.reserve_mode
        and type(expected_control_revision) is int
    )
    additions = validate_addition(previous, candidate, cycle._policy())
    before_ledger = copy.deepcopy(cycle._load())
    ready = [r for r in before_ledger["records"] if r.get("state") == "quality_ready"]
    # Native validators run before acquiring the cross-process configuration lock.
    for record in ready:
        cycle.validate_reserve(record["id"])
    changed = []
    try:
        with core.db.session.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                require(
                    session.scalar(
                        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": WORKER_LOCK}
                    )
                )
            fence_editorial_configuration(session)
            control = session.scalar(
                select(EditorialControl).where(EditorialControl.id == "main").with_for_update()
            )
            require(control is not None and control.revision == expected_control_revision)
            require(
                not session.scalar(
                    select(Dispatch.id)
                    .where(
                        Dispatch.state.in_(["pending", "sending", "unknown", "retry_wait"])
                        | Dispatch.owner.is_not(None)
                        | Dispatch.lease_until.is_(None)
                        | (Dispatch.lease_until != 0)
                    )
                    .limit(1)
                )
            )
            require(
                not session.scalar(
                    select(BridgePost.post_id)
                    .where(
                        BridgePost.mutation_owner.is_not(None)
                        | BridgePost.mutation_until.is_(None)
                        | (BridgePost.mutation_until != 0)
                    )
                    .limit(1)
                )
            )
            require(
                not session.scalar(
                    select(EditorialPreparationClaim.id)
                    .where(EditorialPreparationClaim.active_key.is_not(None))
                    .limit(1)
                )
            )
            require(
                not session.scalar(
                    select(PublicationSlot.id)
                    .where(PublicationSlot.active_target.is_not(None))
                    .limit(1)
                )
            )
            ledger = session.scalar(
                select(EditorialLedger).where(EditorialLedger.id == "hourly").with_for_update()
            )
            require(ledger is not None and ledger.payload == before_ledger)
            require(
                not session.scalar(
                    select(MobileOperation.id).where(MobileOperation.state == "running").limit(1)
                )
            )
            require(
                all(
                    r.get("state") in TERMINAL | {"quality_ready", "awaiting_human_review"}
                    for r in ledger.payload["records"]
                )
            )
            payload = copy.deepcopy(ledger.payload)
            targets = {t.id: t for t in session.scalars(select(Target).with_for_update())}
            configured = {t["id"]: t for t in core.publishing["targets"]}
            require(set(targets) == set(configured))
            target_changes = {}
            for ident, target in targets.items():
                expected = fingerprint(
                    {"target": configured[ident], "editorial_config": previous.hash}
                )
                require(target.binding_hash == expected and target.paused is True)
                replacement = fingerprint(
                    {"target": configured[ident], "editorial_config": candidate.hash}
                )
                target_changes[ident] = {"before": expected, "after": replacement}
                target.binding_hash = replacement
            core.settings = candidate
            for record in payload["records"]:
                if record.get("state") != "quality_ready":
                    continue
                require(
                    not any(
                        record.get(k) is not None for k in ("postiz_id", "dispatch_id", "due_at")
                    )
                )
                variant = core._variant(session, record["variant_id"], True)
                target = targets[variant.target_id]
                require(
                    variant.config_hash == previous.hash
                    and variant.binding_hash == target_changes[target.id]["before"]
                )
                require(
                    not session.scalar(
                        select(TargetApproval.id)
                        .where(TargetApproval.variant_id == variant.id)
                        .limit(1)
                    )
                )
                require(
                    not session.scalar(
                        select(Dispatch.id).where(Dispatch.variant_id == variant.id).limit(1)
                    )
                )
                require(
                    not session.scalar(
                        select(BridgePost.post_id)
                        .where(BridgePost.variant_id == variant.id)
                        .limit(1)
                    )
                )
                original = copy.deepcopy(core.snapshot(variant, target))
                require(
                    record["prepared_hash"]
                    == record["reserve_prepared_hash"]
                    == fingerprint(original)
                )
                evidence = automatic_evidence(session, variant)
                require(evidence is not None)
                draft = session.get(Draft, variant.draft_id)
                require(
                    draft is not None
                    and draft.config_id == previous.hash
                    and draft.revision == variant.source_revision
                )
                draft.revision += 1
                draft.config_id = candidate.hash
                session.add(
                    DraftRevision(
                        draft_id=draft.id,
                        revision=draft.revision,
                        payload=copy.deepcopy(draft.current),
                        config_id=draft.config_id,
                    )
                )
                variant.revision += 1
                variant.source_revision = draft.revision
                variant.config_hash = candidate.hash
                variant.binding_hash = target.binding_hash
                variant.updated_at = now()
                core._quality(session, variant, target)
                require(automatic_evidence(session, variant) == evidence)
                core._history(session, variant, target, "owner_source_activation")
                updated = fingerprint(core.snapshot(variant, target))
                record["prepared_hash"] = record["reserve_prepared_hash"] = updated
                core.db.audit(
                    session,
                    "owner_source_activation",
                    "reserve_source_configuration_rebound",
                    record["id"],
                    previous_snapshot=original,
                    previous_hash=fingerprint(original),
                    new_hash=updated,
                    old_config_hash=previous.hash,
                    new_config_hash=candidate.hash,
                    machine_evidence_hash=evidence["evidence_hash"],
                    model_calls=0,
                    external_calls=0,
                )
                changed.append(record["id"])
            ledger.payload = payload
            ledger.revision += 1
            ledger.updated_at = now()
            core.db.audit(
                session,
                "owner_source_activation",
                "source_configuration_added",
                "main",
                old_config_hash=previous.hash,
                new_config_hash=candidate.hash,
                added_sources=additions,
                rebound_records=changed,
                target_bindings=target_changes,
                previous_ledger_hash=fingerprint(before_ledger),
                ledger_hash=fingerprint(payload),
                approvals_created=0,
                external_calls=0,
            )
    except BaseException:
        core.settings = previous
        raise
    return {
        "added_sources": additions,
        "rebound_records": changed,
        "old_config_hash": previous.hash,
        "new_config_hash": candidate.hash,
        "approvals_created": 0,
        "external_calls": 0,
    }
