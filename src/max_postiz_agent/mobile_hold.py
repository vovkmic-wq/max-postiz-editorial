"""Ручной барьер редакции, общий для панели, Postiz и отправителя."""

import math

from sqlalchemy import select

from .config import AgentError, fingerprint
from .db import Audit, now, uid
from .hub_models import (
    BridgePost,
    Dispatch,
    DispatchAttempt,
    RemoteDraft,
    Target,
    TargetApproval,
    Variant,
)
from .platform_routes import DIRECT_ROUTES
from .server_models import EditorialControl, MobileItemHold, PublicationSlot
from .server_state import CONTROL_ID, fence_editorial_configuration

MAX_OBSERVATION_AGE = 20


def _view(row):
    return {
        "active": row.active if row else False,
        "revision": row.revision if row else 0,
        "reason": row.reason if row else None,
        "actor": row.actor if row else None,
        "updated_at": row.updated_at if row else None,
    }


def item_hold(session, item_id):
    return _view(session.get(MobileItemHold, item_id))


def item_is_held(session, item_id):
    return bool(
        session.scalar(select(MobileItemHold.active).where(MobileItemHold.variant_id == item_id))
    )


def assert_item_unheld(session, item_id):
    if item_is_held(session, item_id):
        raise AgentError("MOBILE_ITEM_HELD", 409)


def _finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def _due_matches(left, right):
    return _finite(left) and _finite(right) and abs(left - right) < 0.001


def _require(condition, code="MOBILE_HOLD_NOT_SAFE"):
    if not condition:
        raise AgentError(code, 409)


def _capture_item_hold(core, session, item_id, action, *, lock=False):
    from .preapproval_retirement import assert_mutable

    if action not in {"hold_item", "release_item_hold"}:
        raise AgentError("MOBILE_HOLD_ACTION_INVALID", 422)

    def current(model, clause):
        query = select(model).where(clause)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return session.scalar(query)

    control = current(EditorialControl, EditorialControl.id == CONTROL_ID)
    _require(control is not None, "EDITORIAL_NOT_INITIALIZED")
    variant = current(Variant, Variant.id == item_id)
    if variant is None:
        raise AgentError("MOBILE_HOLD_ITEM_NOT_FOUND", 404)
    assert_mutable(session, variant.id)
    target = current(Target, Target.id == variant.target_id)
    hold = current(MobileItemHold, MobileItemHold.variant_id == variant.id)
    hold_view = _view(hold)
    _require(hold_view["active"] is (action == "release_item_hold"), "MOBILE_HOLD_STATE_CONFLICT")
    _require(
        target is not None
        and target.enabled
        and target.destination
        and target.binding_hash == variant.binding_hash,
        "MOBILE_HOLD_ROUTE_UNAVAILABLE",
    )

    # Блокируем только текущий снимок до первой попытки. История и неизвестный
    # исход сохраняют свой статус после истечения lease.
    dispatch = current(Dispatch, Dispatch.variant_id == variant.id)
    approval = current(TargetApproval, TargetApproval.variant_id == variant.id)
    snapshot = core.snapshot(variant, target)
    snapshot_hash = fingerprint(snapshot)
    dispatch_id, due = None, None
    if dispatch is not None:
        dispatch_id, due = dispatch.id, dispatch.due_at
        signed = fingerprint({"content": snapshot, "due_at": due})
        _require(
            dispatch.state == "pending"
            and dispatch.owner is None
            and dispatch.lease_until == 0
            and dispatch.attempts == 0
            and dispatch.verified is False
            and dispatch.error is None
            and dispatch.external_id is None
            and dispatch.permalink is None
            and _finite(due)
            and dispatch.revision == variant.revision
            and dispatch.snapshot == snapshot
            and dispatch.snapshot_hash == signed
            and variant.state == "approved"
            and approval is not None
            and approval.id == dispatch.approval_id
            and not approval.revoked
            and approval.revision == variant.revision
            and approval.scheduled_at == due
            and approval.snapshot_hash == signed
            and session.scalar(
                select(Dispatch.id)
                .where(Dispatch.variant_id == variant.id, Dispatch.id != dispatch.id)
                .limit(1)
            )
            is None
            and session.scalar(
                select(TargetApproval.id)
                .where(TargetApproval.variant_id == variant.id, TargetApproval.id != approval.id)
                .limit(1)
            )
            is None
            and session.scalar(
                select(DispatchAttempt.id)
                .where(DispatchAttempt.dispatch_id == dispatch.id)
                .limit(1)
            )
            is None
        )
        # Снимаем барьер, пока подписанная дата в будущем. Пропущенный слот
        # требует отдельного решения.
        if action == "release_item_hold":
            _require(due > now(), "MOBILE_HOLD_DUE_ELAPSED")
    else:
        _require(approval is None and variant.state == "review_pending")

    # Барьер сохраняет слот. Если он есть, назначение должно точно совпадать
    # с этой записью и ожидать первой попытки.
    record_id = variant.metadata_json.get("record_id")
    slot = None
    if isinstance(record_id, str) and record_id:
        slot = session.scalar(
            select(PublicationSlot).where(
                PublicationSlot.target_id == target.id, PublicationSlot.record_id == record_id
            )
        )
    if slot is not None:
        _require(
            dispatch is not None
            and slot.state == "pending"
            and slot.active_target == target.id
            and slot.dispatch_id == dispatch.id
            and slot.due_at == due
            and slot.error is None
            and slot.delivered_at is None
            and isinstance(slot.binding, dict)
            and slot.binding.get("variant_id") == variant.id
        )

    bridge = current(BridgePost, BridgePost.variant_id == variant.id)
    draft = current(RemoteDraft, RemoteDraft.variant_id == variant.id)
    remote = None
    if target.route == "max_browser" and target.platform == "max":
        _require(core.bridge.enabled, "MOBILE_HOLD_ROUTE_UNAVAILABLE")
        expected_state = "QUEUE" if dispatch is not None else "DRAFT"
        _require(
            bridge is not None
            and bridge.active
            and bridge.error is None
            and bridge.target_id == target.id
            and bridge.post_id
            and bridge.group_id
            and bridge.integration_id
            and bridge.integration_id == target.options.get("postiz_integration_id")
            and bridge.mutation_owner is None
            and bridge.mutation_until == 0
            and bridge.remote_state == expected_state
            and variant.metadata_json.get("postiz_state") == expected_state
            and variant.metadata_json.get("postiz_post_id") == bridge.post_id
            and variant.metadata_json.get("postiz_hash") == bridge.remote_hash
            and variant.metadata_json.get("postiz_due_at") == bridge.due_at
            and _finite(bridge.updated_at)
            and _finite(bridge.due_at)
            and (dispatch is None or _due_matches(bridge.due_at, due))
        )
        _require(draft is None or draft.state == "draft" and draft.external_id == bridge.post_id)
        remote = {
            "id": bridge.post_id,
            "state": expected_state,
            "group": bridge.group_id,
            "integration_id": bridge.integration_id,
            "hash": bridge.remote_hash,
            "due": bridge.due_at,
            "updated_at": bridge.updated_at,
            "destination": target.destination,
            "correlation": draft.correlation if draft else None,
        }
        delivery_path = "postiz_max_browser"
    else:
        _require(
            target.route in DIRECT_ROUTES
            and DIRECT_ROUTES[target.route] == target.platform
            and bridge is None
            and draft is None
            and not target.options.get("postiz_integration_id")
            and not any(
                key in variant.metadata_json
                for key in ("postiz_post_id", "postiz_hash", "postiz_state")
            ),
            "MOBILE_HOLD_ROUTE_UNAVAILABLE",
        )
        delivery_path = "project_dispatch"

    captured = {
        "action": action,
        "item_id": variant.id,
        "target_id": target.id,
        "control_revision": control.revision,
        "target_paused": target.paused,
        "route_options_hash": fingerprint(target.options),
        "variant_revision": variant.revision,
        "snapshot_hash": snapshot_hash,
        "dispatch_id": dispatch_id,
        "due": due,
        "approval_id": approval.id if approval else None,
        "slot_id": slot.id if slot else None,
        "slot_updated_at": slot.updated_at if slot else None,
        "hold_revision": hold_view["revision"],
        "delivery_path": delivery_path,
        "remote": remote,
    }
    return {**captured, "fence": fingerprint(captured)}


def capture_item_hold(core, session, item_id, action):
    """Локальное условие для плана. Не выполняет HTTP и не меняет состояние."""
    return _capture_item_hold(core, session, item_id, action)


def verify_item_hold(core, captured):
    """Убедиться, что внешняя очередь принадлежит именно MaxBrowserProvider."""
    expected = captured["remote"]
    if expected is not None:
        from .max_bridge import PROVIDER

        matches = [
            row
            for row in core.postiz.integrations_fresh()
            if isinstance(row, dict) and row.get("id") == expected["integration_id"]
        ]
        _require(
            len(matches) == 1
            and matches[0].get("identifier") == PROVIDER
            and matches[0].get("accountId") == expected["destination"]
            and matches[0].get("disabled") is False
            and matches[0].get("refreshNeeded") is False,
            "MOBILE_HOLD_PROVIDER_UNVERIFIED",
        )
        remote = core.postiz.get_post_fresh(expected["id"])
        _require(
            isinstance(remote, dict)
            and isinstance(remote.get("integration"), dict)
            and isinstance(remote.get("publishDate"), str),
            "MOBILE_HOLD_REMOTE_CHANGED",
        )
        _, _, due, remote_hash = core.bridge.normalize(remote)
        settings = core.bridge._remote_settings(remote)
        marked = "hub_variant_id" in settings or "hub_correlation" in settings
        integration = remote["integration"]
        _require(
            remote["id"] == expected["id"]
            and remote["state"] == expected["state"]
            and remote["group"] == expected["group"]
            and integration["id"] == expected["integration_id"]
            and integration.get("providerIdentifier", PROVIDER) == PROVIDER
            and remote_hash == expected["hash"]
            and due == expected["due"]
            and (captured["due"] is None or _due_matches(due, captured["due"]))
            and remote.get("releaseId") in (None, "")
            and remote.get("releaseURL") in (None, "")
            and settings.get("__type", PROVIDER) == PROVIDER
            and (
                not marked
                or expected["correlation"] is not None
                and settings.get("hub_variant_id") == captured["item_id"]
                and settings.get("hub_correlation") == expected["correlation"]
            ),
            "MOBILE_HOLD_REMOTE_CHANGED",
        )
    return {"fence": captured["fence"], "at": now()}


def apply_item_hold(core, session, captured, verified, *, actor, reason):
    """Подтвердить тот же снимок и записать барьер с аудитом в одной транзакции."""
    reason = reason.strip() if isinstance(reason, str) else ""
    if not 3 <= len(reason) <= 512 or not reason.isprintable():
        raise AgentError("MOBILE_HOLD_REASON_REQUIRED", 422)
    _require(
        verified.get("fence") == captured["fence"]
        and _finite(verified.get("at"))
        and 0 <= now() - verified["at"] <= MAX_OBSERVATION_AGE,
        "MOBILE_HOLD_OBSERVATION_STALE",
    )
    # Порядок блокировок общий с Postiz и отправителем. Внешнее чтение уже
    # завершено. Если отправитель успел взять lease, установка даст конфликт.
    fence_editorial_configuration(session)
    fresh = _capture_item_hold(core, session, captured["item_id"], captured["action"], lock=True)
    _require(fresh["fence"] == captured["fence"], "MOBILE_HOLD_REVISION_CONFLICT")
    _require(0 <= now() - verified["at"] <= MAX_OBSERVATION_AGE, "MOBILE_HOLD_OBSERVATION_STALE")
    row = session.get(MobileItemHold, fresh["item_id"])
    if row is None:
        row = MobileItemHold(variant_id=fresh["item_id"])
        session.add(row)
    row.active = fresh["action"] == "hold_item"
    row.revision = fresh["hold_revision"] + 1
    row.reason, row.actor, row.updated_at = reason, actor, now()
    audit_id = uid()
    session.add(
        Audit(
            id=audit_id,
            actor=actor,
            action=fresh["action"],
            entity=row.variant_id,
            details={
                "target_id": fresh["target_id"],
                "revision": row.revision,
                "variant_revision": fresh["variant_revision"],
                "reason": reason,
                "active": row.active,
                "delivery_path": fresh["delivery_path"],
                "dispatch_id": fresh["dispatch_id"],
                "external_mutations": 0,
            },
        )
    )
    return {**_view(row), "audit_id": audit_id, "delivery_path": fresh["delivery_path"]}
