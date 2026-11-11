"""Server-authoritative Android projections and guarded editorial operations."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select

from . import __version__
from .article_cycle import HourlyArticleCycle
from .article_reserve import ArticleReserveStore
from .config import AgentError, fingerprint
from .db import Audit, now
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from .platform_management import _evidence_kind, reconcile
from .platform_routes import DIRECT_ROUTES
from .server_api import reddit_capability
from .server_models import EditorialLedger, EditorialPreparationClaim, PublicationSlot
from .server_state import CONTROL_ID, LEDGER_ID, ServerState, fence_editorial_configuration

_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_MAX_CALENDAR_DAYS = 31
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,99}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_QUEUE_STATES = {
    "assigned", "awaiting_manual", "awaiting_postiz", "automatic_pending_approval",
    "automatic_ready", "blocked_unknown", "cancelled", "delivered", "draft", "failed",
    "missed", "needs_attention", "owner_attested", "pending", "preparing", "published",
    "published_unverified", "quality_ready", "ready", "rejected", "retry_wait",
    "review_pending", "scheduled", "sending", "staging_reserve_draft", "unknown", "unresolved",
    "DRAFT", "ERROR", "PUBLISHED", "QUEUE",
}
_SUPPORTED_ROUTES = {"max_browser", *DIRECT_ROUTES}
_LINK_HOSTS = {"max.ru", "web.max.ru", "t.me", "telegram.me", "vk.com", "vk.ru",
               "m.vk.com", "pikabu.ru", "www.pikabu.ru"}
_DISPATCH_BUSY = or_(
    Dispatch.state.in_(("sending", "unknown")), Dispatch.owner.is_not(None),
    Dispatch.lease_until.is_(None), Dispatch.lease_until != 0,
)
_BRIDGE_BUSY = or_(
    BridgePost.mutation_owner.is_not(None), BridgePost.mutation_until.is_(None),
    BridgePost.mutation_until != 0,
)


def _timestamp(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise AgentError("MOBILE_DATE_INVALID", 422)
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise AgentError("MOBILE_DATE_INVALID", 422) from exc


def _code(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _remote_link(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in "\r\n\0"):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname in _LINK_HOSTS
                and parsed.port in {None, 443} and not parsed.username and not parsed.password):
            return parsed._replace(query="", fragment="").geturl()
    except ValueError:
        pass
    return None


def _require(value, code, status=409):
    if not value:
        raise AgentError(code, status)


def _pagination(resource, filters, keys):
    _require(not (set(filters) - {"cursor", "limit", *keys}), "MOBILE_FILTER_INVALID", 422)
    limit = filters.get("limit", "30")
    _require(isinstance(limit, str) and re.fullmatch(r"[1-9][0-9]?", limit),
             "MOBILE_FILTER_INVALID", 422)
    digest = fingerprint([resource, {k: filters[k] for k in keys if k in filters}])
    cursor = filters.get("cursor")
    after = ""
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,640}", cursor):
                raise ValueError("cursor")
            version, identity, after = json.loads(urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if version != 1 or identity != digest or not isinstance(after, str) or len(after) > 300:
                raise ValueError("cursor")
        except (ValueError, TypeError, Base64Error) as exc:
            raise AgentError("MOBILE_CURSOR_INVALID", 422) from exc
    return int(limit), digest, after


def _page(items, limit, digest):
    cursor = None
    if len(items) > limit:
        cursor = urlsafe_b64encode(json.dumps([1, digest, items[limit - 1][0]]).encode())
        cursor = cursor.decode().rstrip("=")
    return {"items": [value for _, value in items[:limit]], "next_cursor": cursor}


class MobileDomain:
    def __init__(self, core, *, unavailable_browser_routes=()):
        self.unavailable_browser_routes = frozenset(unavailable_browser_routes)
        self.core = core
        self.store = ServerState(core)

    def read(self, resource, *, object_id=None, filters):
        if resource == "targets" and object_id is None:
            return self._targets(filters)
        if resource == "queue" and object_id is None:
            return self._queue(filters)
        if resource == "calendar" and object_id is None:
            return self._calendar(filters)
        _require(not filters, "MOBILE_FILTER_INVALID", 422)
        if resource == "overview" and object_id is None:
            return self._overview()
        if resource == "reserve" and object_id is None:
            return self._reserve()
        if resource in {"queue", "dispatches"} and object_id:
            return self._detail(resource, object_id)
        raise AgentError("MOBILE_RESOURCE_NOT_FOUND", 404)

    def _overview(self):
        state = self.store.read()
        runtime = state["runtime"]
        preparation = runtime.get("preparation") or {}
        blocked = []
        with self.core.db.session() as session:
            unresolved = session.scalar(select(func.count()).select_from(Dispatch).where(
                Dispatch.state.in_(("sending", "unknown"))))
            if state["paused"]:
                try:
                    self._resume_project_guard(session)
                except AgentError as error:
                    blocked.append(error.code)
        return {
            "version": __version__, "revision": state["revision"], "paused": state["paused"],
            "phase": "paused" if state["paused"] else runtime.get("phase", "idle"),
            "heartbeat_at": _timestamp(runtime.get("heartbeat_at")),
            "next_run_at": _timestamp(runtime.get("next_run_at")),
            "observed_at": _timestamp(now()), "updated_at": _timestamp(state["updated_at"]),
            "preparation": {"phase": preparation.get("phase"),
                            "heartbeat_at": _timestamp(preparation.get("heartbeat_at"))},
            "unresolved_dispatches": unresolved, "blocked_reasons": blocked,
            "available_actions": [] if blocked else ["resume_project" if state["paused"] else "pause_project"],
        }

    def _reserve(self):
        status = ArticleReserveStore(self.core).status()
        settings = self.store.read()["settings"]
        with self.core.db.session() as session:
            records = self._records(session)
        reserve = settings.get("reserve") or {}
        return {
            "enabled": reserve.get("enabled", False), "target": reserve.get("target"),
            "ready": sum(record.get("state") == "quality_ready" for record in records),
            "slots": status["slots"], "observed_at": _timestamp(now()),
            "last_delivered_at": _timestamp(status["last_delivered_at"]),
            "next_delivery_at": _timestamp(status["next_delivery_at"]),
        }

    @staticmethod
    def _records(session):
        ledger = session.get(EditorialLedger, LEDGER_ID)
        return ledger.payload["records"] if ledger else []

    def _targets(self, filters):
        limit, digest, after = _pagination("targets", filters, ())
        control = self.store.read()
        with self.core.db.session() as session:
            targets = list(session.scalars(select(Target).where(Target.id > after)
                                           .order_by(Target.id).limit(limit + 1)))
            return _page([(target.id, self._target_view(session, control, target))
                          for target in targets], limit, digest)

    def _target_view(self, session, control, target):
        action = "resume_target" if target.paused else "pause_target"
        blocked = []
        try:
            self._target_plan(session, control, target, action)
        except AgentError as error:
            blocked.append(error.code)
        return {"id": target.id, "label": target.id, "platform": target.platform,
                "route": target.route, "paused": target.paused, "enabled": target.enabled,
                "blocked_reasons": blocked, "available_actions": [] if blocked else [action]}

    @staticmethod
    def _dispatch(session, variant):
        rows = list(session.scalars(select(Dispatch).where(Dispatch.variant_id == variant.id)
                                    .order_by(Dispatch.state.in_(ACTIVE).desc(),
                                              Dispatch.due_at.desc(), Dispatch.id.desc()).limit(2)))
        return rows[0] if rows else None, len(rows) == 2 and all(r.state in ACTIVE for r in rows)

    @staticmethod
    def _bridge(session, variant_id):
        return session.scalar(select(BridgePost).where(BridgePost.variant_id == variant_id))

    @staticmethod
    def _evidence(session, dispatch):
        if dispatch is None:
            return None
        audits = list(session.scalars(select(Audit).where(
            Audit.entity == dispatch.id,
            Audit.action.in_(("delivery_evidence", "route_reconcile_read_only")))
            .order_by(Audit.at.desc(), Audit.id.desc()).limit(30)))
        kind = _evidence_kind(dispatch, audits)
        return {"kind": kind, "verified": dispatch.state == "published" and kind == "independent_readback",
                "server_verified": dispatch.verified is True, "permalink": _remote_link(dispatch.permalink)}

    def _item(self, session, variant=None, record=None, *, dispatch=None, detail=False):
        from .mobile_hold import capture_item_hold, item_hold

        record = record or {}
        ambiguous = False
        if variant is not None and dispatch is None:
            dispatch, ambiguous = self._dispatch(session, variant)
        bridge = self._bridge(session, variant.id) if variant else None
        slot = (session.scalar(select(PublicationSlot).where(PublicationSlot.record_id == record["id"]))
                if record.get("id") else None)
        hold = item_hold(session, variant.id) if variant else {"active": False}
        blocked = {_code(record.get("error")), _code(dispatch.error) if dispatch else None,
                   _code(slot.error) if slot else None, _code(bridge.error) if bridge else None}
        if hold["active"]:
            blocked.add("MOBILE_ITEM_HELD")
        if ambiguous:
            blocked.add("MOBILE_DISPATCH_LINK_AMBIGUOUS")
        if dispatch and dispatch.state in {"sending", "unknown"}:
            blocked.add("ARTICLE_RECONCILIATION_REQUIRED")
        if bridge and (bridge.mutation_owner is not None or bridge.mutation_until != 0):
            blocked.add("BRIDGE_EDIT_IN_PROGRESS")
        if slot and slot.state == "blocked_unknown":
            blocked.add("ARTICLE_RECONCILIATION_REQUIRED")
        actions = []
        if not ambiguous and dispatch and dispatch.state in {"sending", "unknown"}:
            route = dispatch.snapshot.get("route")
            if route in self.unavailable_browser_routes:
                blocked.add("BROWSER_WORKER_CGROUP_UNAVAILABLE")
            elif route in _SUPPORTED_ROUTES:
                actions.append("reconcile_dispatch")
        if (slot and slot.state == "blocked_unknown" and not slot.dispatch_id and not record.get("schedule_journal")
                and (record.get("error") == "ARTICLE_REMOTE_CHANGED"
                     or (record.get("error") == "POSTIZ_UNKNOWN" and bridge and bridge.mutation_owner
                         and type(bridge.mutation_until) in {int, float}
                         and 0 < bridge.mutation_until <= now() and slot.due_at <= now()))):
            actions.append("retire_preapproval_no_send")
        if variant and not ambiguous and not record.get("no_send_disposition"):
            action = "release_item_hold" if hold["active"] else "hold_item"
            try:
                capture_item_hold(self.core, session, variant.id, action)
            except AgentError as error:
                if detail:
                    blocked.add(_code(error.code))
            else:
                actions.append(action)
        scheduled_at = (dispatch.due_at if dispatch else slot.due_at if slot
                        else bridge.due_at if bridge else record.get("due_at"))
        result = {
            "id": variant.id if variant else record["id"],
            "title": (dispatch.snapshot.get("title", "") if dispatch else variant.title if variant else
                      str(record.get("title") or record.get("source_id") or "Статья"))[:256],
            "target_id": variant.target_id if variant else record.get("target_id"),
            "scheduled_at": _timestamp(scheduled_at),
            "revision": dispatch.revision if dispatch else variant.revision if variant else None,
            "snapshot_hash": dispatch.snapshot_hash if dispatch else
            fingerprint(self.core.snapshot(variant, self.core._target(session, variant.target_id)))
            if variant else None,
            "editorial_state": record.get("state") or (variant.state if variant else None),
            "postiz_state": bridge.remote_state if bridge else record.get("postiz_state"),
            "dispatch_state": dispatch.state if dispatch else None,
            "slot_state": slot.state if slot else None, "delivery_evidence": self._evidence(session, dispatch),
            "blocked_reasons": sorted(blocked - {None}), "available_actions": actions,
            "dispatch_id": dispatch.id if dispatch else None, "record_id": record.get("id"),
            "postiz_id": bridge.post_id if bridge else None,
        }
        if detail:
            result["hold"] = {"active": hold["active"], "revision": hold.get("revision", 0)}
            result["attempts"] = [
                {"id": row.id, "result": row.result, "error": _code(row.error),
                 "created_at": _timestamp(row.created_at)}
                for row in session.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id)
                                           .order_by(DispatchAttempt.created_at.desc(), DispatchAttempt.id)
                                           .limit(50))
            ] if dispatch else []
        return result

    def _detail(self, resource, object_id):
        _require(isinstance(object_id, str) and _ID.fullmatch(object_id),
                 "MOBILE_OBJECT_NOT_FOUND", 404)
        with self.core.db.session() as session:
            records = self._records(session)
            if resource == "dispatches":
                dispatch = session.get(Dispatch, object_id)
                _require(dispatch is not None, "MOBILE_OBJECT_NOT_FOUND", 404)
                variant = session.get(Variant, dispatch.variant_id)
            else:
                dispatch = None
                variant = session.get(Variant, object_id)
            record = next((record for record in records if
                           (variant and record.get("variant_id") == variant.id)
                           or (resource == "queue" and record.get("id") == object_id)), None)
            _require(variant is not None or record is not None, "MOBILE_OBJECT_NOT_FOUND", 404)
            return self._item(session, variant, record, dispatch=dispatch, detail=True)

    def _queue(self, filters):
        limit, digest, after = _pagination("queue", filters, ("target_id", "state", "from", "to"))
        target_id, state, start, end = self._queue_filters(filters)
        with self.core.db.session() as session:
            records = self._records(session)
            record_by_variant = {r["variant_id"]: r for r in records if r.get("variant_id")}
            orphans = [(f"0:{r['id']}", r) for r in records
                       if not r.get("variant_id") and f"0:{r['id']}" > after
                       and (target_id is None or r.get("target_id") == target_id)]
            items = []
            for key, record in sorted(orphans):
                value = self._item(session, record=record)
                if self._queue_match(value, state, start, end):
                    items.append((key, value))
                    if len(items) > limit:
                        return _page(items, limit, digest)
            query = select(Variant).order_by(Variant.id).execution_options(yield_per=100)
            if after.startswith("1:"):
                query = query.where(Variant.id > after[2:])
            if target_id:
                query = query.where(Variant.target_id == target_id)
            for variant in session.scalars(query):
                value = self._item(session, variant, record_by_variant.get(variant.id))
                if self._queue_match(value, state, start, end):
                    items.append((f"1:{variant.id}", value))
                    if len(items) > limit:
                        break
            return _page(items, limit, digest)

    @staticmethod
    def _queue_filters(filters):
        state, target_id = filters.get("state"), filters.get("target_id")
        _require(state is None or state in _QUEUE_STATES, "MOBILE_FILTER_INVALID", 422)
        _require(target_id is None or isinstance(target_id, str) and _ID.fullmatch(target_id),
                 "MOBILE_FILTER_INVALID", 422)
        start = _parse_time(filters["from"]) if "from" in filters else None
        end = _parse_time(filters["to"]) if "to" in filters else None
        _require(start is None or end is None or start < end, "MOBILE_DATE_INVALID", 422)
        return target_id, state, start, end

    @staticmethod
    def _queue_match(value, state, start, end):
        states = {value["editorial_state"], value["dispatch_state"],
                  value["postiz_state"], value["slot_state"]}
        if state == "unresolved":
            if not states & {"sending", "unknown", "blocked_unknown", "needs_attention"}:
                return False
        elif state is not None and state not in states:
            return False
        if start is None and end is None:
            return True
        if not value["scheduled_at"]:
            return False
        stamp = _parse_time(value["scheduled_at"])
        return (start is None or stamp >= start) and (end is None or stamp < end)

    def _calendar(self, filters):
        limit, digest, after = _pagination("calendar", filters, ("target_id", "state", "from", "to"))
        target_id, state, start, end = self._queue_filters(filters)
        _require(start is not None and end is not None and end - start <= timedelta(days=_MAX_CALENDAR_DAYS),
                 "MOBILE_CALENDAR_WINDOW_INVALID", 422)
        postiz_error = None
        try:
            remote_posts = self.core.postiz.posts(start, end)
        except AgentError as error:
            postiz_error, remote_posts = _code(error.code) or "POSTIZ_UNAVAILABLE", []
        items = []
        with self.core.db.session() as session:
            records = self._records(session)
            by_variant = {r["variant_id"]: r for r in records if r.get("variant_id")}
            by_record = {r["id"]: r for r in records}
            seen = set()
            for remote in remote_posts:
                if not isinstance(remote, dict) or not isinstance(remote.get("id"), str):
                    raise AgentError("MOBILE_POSTIZ_CALENDAR_INVALID", 503)
                bridge = session.get(BridgePost, remote["id"])
                if bridge is None or target_id and bridge.target_id != target_id:
                    continue
                variant = session.get(Variant, bridge.variant_id)
                if variant is None:
                    continue
                _require(variant.id not in seen, "MOBILE_POSTIZ_CALENDAR_AMBIGUOUS")
                seen.add(variant.id)
                value = self._item(session, variant, by_variant.get(variant.id))
                integration = remote.get("integration")
                if (not isinstance(integration, dict) or bridge.integration_id != integration.get("id")
                        or bridge.group_id != remote.get("group")):
                    value["blocked_reasons"].append("MOBILE_POSTIZ_LINK_UNVERIFIED")
                    value["available_actions"] = []
                else:
                    value["postiz_state"] = remote.get("state") if remote.get("state") in _QUEUE_STATES else None
                    value["scheduled_at"] = _timestamp(_parse_time(remote.get("publishDate")).timestamp())
                    value["postiz_observation"] = "fresh"
                value["calendar_entry_id"] = f"postiz:{bridge.post_id}"
                items.append((f"{value['scheduled_at']}:{value['calendar_entry_id']}", value))

            # Direct routes live in the project's dispatch calendar. Show every
            # planned attempt and reserve slot without manufacturing Postiz rows.
            dispatches = select(Dispatch, Variant).join(Variant).where(
                Dispatch.due_at >= start.timestamp(), Dispatch.due_at < end.timestamp())
            slots = select(PublicationSlot).where(
                PublicationSlot.due_at >= start.timestamp(), PublicationSlot.due_at < end.timestamp())
            if target_id:
                dispatches = dispatches.where(Variant.target_id == target_id)
                slots = slots.where(PublicationSlot.target_id == target_id)
            for dispatch, variant in session.execute(dispatches):
                if variant.id in seen:
                    continue
                value = self._item(session, variant, by_variant.get(variant.id), dispatch=dispatch)
                if value["postiz_id"]:
                    value["postiz_observation"] = "last_saved"
                value["calendar_entry_id"] = f"dispatch:{dispatch.id}"
                items.append((f"{value['scheduled_at']}:{value['calendar_entry_id']}", value))
            for slot in session.scalars(slots):
                record = by_record.get(slot.record_id)
                if not record or slot.dispatch_id or record.get("variant_id") in seen:
                    continue
                variant = session.get(Variant, record.get("variant_id")) if record.get("variant_id") else None
                value = self._item(session, variant, record)
                if value["postiz_id"]:
                    value["postiz_observation"] = "last_saved"
                value["calendar_entry_id"] = f"slot:{slot.id}"
                items.append((f"{value['scheduled_at']}:{value['calendar_entry_id']}", value))
        page = _page(sorted((key, value) for key, value in items if key > after
                            and self._queue_match(value, state, start, end)), limit, digest)
        return {**page, "postiz_available": postiz_error is None, "postiz_error": postiz_error}

    def plan(self, action, object_id):
        _require(isinstance(object_id, str) and _ID.fullmatch(object_id),
                 "MOBILE_OBJECT_NOT_FOUND", 404)
        if action in {"pause_project", "resume_project"}:
            _require(object_id == CONTROL_ID, "MOBILE_OBJECT_NOT_FOUND", 404)
            current = self.store.read()
            _require(current["paused"] == (action == "resume_project"), "MOBILE_CONTROL_UNCHANGED")
            if action == "resume_project":
                self._can_resume_project(current)
            return {"effect": action, "revision": current["revision"],
                    "public": {"revision": current["revision"],
                               "withdraws_existing_queue": False, "automatically_resumes_targets": False}}
        if action in {"pause_target", "resume_target"}:
            with self.core.db.session() as session:
                target = self.core._target(session, object_id)
                current = self.store.read()
                fence = self._target_plan(session, current, target, action)
                return {"effect": action, "fence": fence,
                        "public": {"target_id": target.id, "platform": target.platform,
                                   "withdraws_existing_queue": False}}
        if action in {"hold_item", "release_item_hold"}:
            from .mobile_hold import capture_item_hold, verify_item_hold
            with self.core.db.session() as session:
                capture = capture_item_hold(self.core, session, object_id, action)
            verify_item_hold(self.core, capture)
            return {"effect": action, "capture": capture,
                    "public": {"variant_id": object_id, "hold_revision": capture["hold_revision"],
                               "delivery_path": capture["delivery_path"], "withdraws_existing_queue": False}}
        if action == "reconcile_dispatch":
            frozen, snapshot = self._reconcile_plan(object_id)
            journal_identity = None
            if frozen["route"] == "max_browser":
                journal, _ = self._max_journal(frozen, snapshot)
                journal_identity = self._max_journal_identity(journal)
            return {"effect": action, "fence": frozen, "journal_identity": journal_identity,
                    "public": {"dispatch_id": object_id, "platform": snapshot["platform"],
                               "snapshot_hash": frozen["snapshot_hash"], "external_mutations": 0}}
        if action == "retire_preapproval_no_send":
            from .preapproval_retirement import inspect
            report = inspect(self.core, object_id)
            return {"effect": report["effect"], "domain_plan_hash": report["plan_hash"],
                    "public": {"record_id": object_id, "draft_preserved": True,
                               "historical_source_consumed": True, "external_mutations": 0}}
        raise AgentError("MOBILE_ACTION_UNSUPPORTED", 422)

    def execute(self, action, object_id, plan, *, actor, operation_id, reason):
        _require(isinstance(actor, str) and re.fullmatch(r"postiz:mobile:[a-f0-9]{32}", actor),
                 "MOBILE_ACTOR_INVALID", 403)
        if action in {"pause_project", "resume_project"}:
            _require(self.plan(action, object_id) == plan, "MOBILE_PLAN_CHANGED")
            resume = action == "resume_project"
            result = self.store.control("start" if resume else "pause", plan["revision"], actor,
                                        prepare_only=resume)
            return {"paused": result["paused"], "revision": result["revision"],
                    "withdraws_existing_queue": False}
        if action in {"pause_target", "resume_target"}:
            return self._apply_target(action, object_id, plan, actor)
        if action in {"hold_item", "release_item_hold"}:
            from .mobile_hold import apply_item_hold, capture_item_hold, verify_item_hold
            with self.core.db.session() as session:
                capture = capture_item_hold(self.core, session, object_id, action)
            _require(capture == plan["capture"], "MOBILE_PLAN_CHANGED")
            verified = verify_item_hold(self.core, capture)
            with self.core.db.session.begin() as session:
                applied = apply_item_hold(self.core, session, capture, verified, actor=actor, reason=reason)
            return {**applied, "updated_at": _timestamp(applied.get("updated_at"))}
        if action == "reconcile_dispatch":
            fence, snapshot = self._reconcile_plan(object_id)
            _require(fence == plan["fence"], "MOBILE_PLAN_CHANGED")
            if fence["route"] == "max_browser":
                return self._reconcile_max(fence, snapshot, plan["journal_identity"], actor)
            result = reconcile(self.core, object_id, actor=actor, require_independent=True,
                               expected_snapshot_hash=fence["snapshot_hash"])
            return {"dispatch_id": object_id, "dispatch_state": result["state"],
                    "outcome": "delivery_confirmed" if result["verified"] else "proof_insufficient",
                    "evidence_kind": result["evidence_kind"], "verified": result["verified"],
                    "permalink": _remote_link(result["permalink"]), "external_mutations": 0}
        if action == "retire_preapproval_no_send":
            return self._retire(object_id, plan["domain_plan_hash"], actor, operation_id, reason)
        raise AgentError("MOBILE_ACTION_UNSUPPORTED", 422)

    def _can_resume_project(self, current):
        policy = HourlyArticleCycle(self.core)._policy()
        _require(policy.get("approval_mode") == "automatic", "EDITORIAL_AUTOMATIC_POLICY_REQUIRED")
        if current["settings"].get("source") == "reddit":
            _require(reddit_capability(self.core)["available"], "REDDIT_APPROVAL_REQUIRED")
        with self.core.db.session() as session:
            self._resume_project_guard(session)

    @staticmethod
    def _resume_project_guard(session):
        blockers = (select(PublicationSlot.id).where(PublicationSlot.state == "blocked_unknown"),
                    select(Dispatch.id).where(_DISPATCH_BUSY),
                    select(BridgePost.post_id).where(_BRIDGE_BUSY))
        _require(not any(session.scalar(query.limit(1)) is not None for query in blockers),
                 "ARTICLE_RECONCILIATION_REQUIRED")
        _require(not session.scalar(select(EditorialPreparationClaim.id).where(
            EditorialPreparationClaim.active_key.is_not(None)).limit(1)), "ARTICLE_RESERVE_PREPARATION_ACTIVE")
        _require(not session.scalar(select(Target.id).where(Target.paused.is_(False)).limit(1)),
                 "MOBILE_TARGETS_MUST_BE_PAUSED")

    @staticmethod
    def _busy_target(session, target_id):
        checks = (
            select(Dispatch.id).join(Variant).where(Variant.target_id == target_id, _DISPATCH_BUSY),
            select(BridgePost.post_id).where(BridgePost.target_id == target_id, _BRIDGE_BUSY),
            select(PublicationSlot.id).where(PublicationSlot.target_id == target_id,
                                             PublicationSlot.state == "blocked_unknown"),
        )
        return any(session.scalar(check.limit(1)) is not None for check in checks)

    def _target_plan(self, session, control, target, action):
        resume = action == "resume_target"
        _require(target.route in _SUPPORTED_ROUTES, "TARGET_ROUTE_UNSUPPORTED")
        _require(target.paused == resume, "MOBILE_CONTROL_UNCHANGED")
        if resume:
            _require(target.enabled, "TARGET_DISABLED")
            _require(not control["paused"], "EDITORIAL_PAUSED")
            _require(not self._busy_target(session, target.id), "ARTICLE_RECONCILIATION_REQUIRED")
        return {"target_id": target.id, "control_revision": control["revision"],
                "binding_hash": target.binding_hash, "enabled": target.enabled, "paused": target.paused}

    def _apply_target(self, action, object_id, plan, actor):
        from .preapproval_retirement import assert_no_unfinished
        from .server_maintenance import guard_restart_operation
        from .server_models import EditorialControl
        from .server_route_admin import guard_login_resume

        resume = action == "resume_target"
        with self.core.db.session.begin() as session:
            fence_editorial_configuration(session)
            control = session.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            target = session.scalar(select(Target).where(Target.id == object_id).with_for_update())
            _require(control is not None, "EDITORIAL_NOT_INITIALIZED")
            _require(target is not None, "TARGET_NOT_FOUND", 404)
            current = {"revision": control.revision, "paused": control.paused}
            _require(self._target_plan(session, current, target, action) == plan["fence"],
                     "MOBILE_PLAN_CHANGED")
            if resume:
                self.core._assert_current_route_runtime()
                guard_restart_operation(self.core)
                assert_no_unfinished(session)
                if target.route in {"max_browser", "vk_browser"}:
                    guard_login_resume(self.core, target)
            target.paused = not resume
            self.core.db.audit(session, actor, "target_pause", target.id, paused=target.paused)
        return {"target_id": object_id, "paused": not resume, "withdraws_existing_queue": False}

    def _reconcile_plan(self, dispatch_id):
        with self.core.db.session() as session:
            dispatch = session.get(Dispatch, dispatch_id)
            _require(dispatch is not None, "DISPATCH_NOT_FOUND", 404)
            variant = session.get(Variant, dispatch.variant_id)
            _require(variant is not None, "VARIANT_NOT_FOUND", 404)
            target = self.core._target(session, variant.target_id)
            _require(dispatch.state in {"sending", "unknown"}, "ROUTE_RECONCILE_STATE_INVALID")
            _require(math.isfinite(dispatch.due_at) and dispatch.due_at <= now(), "MOBILE_DISPATCH_DATE_INVALID")
            _require(target.route in _SUPPORTED_ROUTES, "TARGET_ROUTE_UNSUPPORTED")
            _require(target.route not in self.unavailable_browser_routes,
                     "BROWSER_WORKER_CGROUP_UNAVAILABLE")
            if dispatch.owner is not None or dispatch.state == "sending":
                can_expire = (type(dispatch.lease_until) in {int, float} and dispatch.lease_until <= now()
                              and (dispatch.state == "sending" or dispatch.error == "SEND_LEASE_EXPIRED"))
                _require(can_expire, "MOBILE_DISPATCH_LEASE_ACTIVE")
            else:
                _require(dispatch.lease_until == 0, "MOBILE_DISPATCH_LEASE_ACTIVE")
            fence = self._dispatch_fence(session, dispatch, variant, target)
            return {**fence, "state": dispatch.state, "owner": dispatch.owner,
                    "lease_until": dispatch.lease_until}, dispatch.snapshot

    def _dispatch_fence(self, session, dispatch, variant, target, *, lock=False):
        approval_query = select(TargetApproval).where(TargetApproval.id == dispatch.approval_id)
        bridge_query = select(BridgePost).where(BridgePost.variant_id == variant.id)
        approval = session.scalar(approval_query.with_for_update() if lock else approval_query)
        bridge = session.scalar(bridge_query.with_for_update() if lock else bridge_query)
        current = fingerprint({"content": self.core.snapshot(variant, target), "due_at": dispatch.due_at})
        frozen = fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
        _require(approval is not None and not approval.revoked and approval.variant_id == variant.id
                 and approval.scheduled_at == dispatch.due_at and approval.revision == variant.revision
                 and dispatch.revision == variant.revision
                 and approval.snapshot_hash == current == frozen == dispatch.snapshot_hash, "APPROVAL_STALE")
        _require(bridge is None or (bridge.mutation_owner is None and bridge.mutation_until == 0),
                 "BRIDGE_EDIT_IN_PROGRESS")
        _require(bridge is None or target.route == "max_browser", "TARGET_ROUTE_UNSUPPORTED")
        return {"dispatch_id": dispatch.id, "variant_id": variant.id, "target_id": target.id,
                "route": target.route, "destination": target.destination, "due_at": dispatch.due_at,
                "binding_hash": target.binding_hash, "snapshot_hash": frozen, "approval_id": approval.id,
                "bridge": None if bridge is None else [bridge.post_id, bridge.remote_hash, bridge.remote_state]}

    def _max_journal(self, fence, snapshot):
        from .browser_publisher import BrowserMax
        from .browser_worker import _snapshot_request
        from .config import parse_json
        from .server_route_admin import plain, read_local

        _require(isinstance(self.core.publisher, BrowserMax), "MOBILE_MAX_READBACK_UNSUPPORTED")
        code = "MOBILE_MAX_READBACK_PROOF_INVALID"
        dispatch_id = fence["dispatch_id"]
        relative = f"data/browser-publications/{dispatch_id}.json"
        try:
            path = plain(self.core.settings.root, relative)
            _require(not path.with_suffix(".new").exists(), code)
            raw = read_local(self.core.settings.root, relative, _MAX_RECEIPT_BYTES)
            journal = parse_json(raw.decode("utf-8"))
            expected, presentation = _snapshot_request(
                self.core.settings, dispatch_id, snapshot, fence["destination"])
            snapshot_hash = fingerprint(snapshot)
            payload = {**expected, **presentation, "dispatch_snapshot_hash": snapshot_hash}
            _require(journal.get("contract") == "browser-send-journal-v1"
                     and journal.get("state") in {"sending", "unknown", "confirmed"}
                     and journal.get("send_started") is True
                     and journal.get("request_id") == dispatch_id
                     and journal.get("chat_id") == fence["destination"]
                     and journal.get("config_hash") == self.core.settings.hash
                     and journal.get("snapshot_hash") == snapshot_hash
                     and journal.get("base_payload") == expected
                     and journal.get("base_payload_hash") == fingerprint(expected)
                     and journal.get("request_payload") == payload
                     and journal.get("content_hash") == fingerprint(payload)
                     and journal.get("presentation") == presentation
                     and journal.get("text") == expected["text"]
                     and journal.get("body_sha256") == hashlib.sha256(expected["text"].encode()).hexdigest()
                     and journal.get("request_attachment") == expected["attachment"]
                     and journal.get("attachment") is bool(expected["attachment"])
                     and journal.get("attachment_type") == (expected["attachment"] or {}).get("type")
                     and type(journal.get("before_count")) is int and journal["before_count"] >= 0
                     and type(journal.get("started_at")) in {int, float}
                     and fence["due_at"] <= journal["started_at"] <= now(), code)
            if expected["attachment"]:
                # Only illustrated MAX has a proof binding a visible image to the
                # original uploaded content and the exact approved presentation.
                _require(bool(presentation), "MOBILE_MAX_READBACK_UNSUPPORTED")
                _require(journal.get("illustration_image_sha256") ==
                         expected["attachment"]["payload"]["sha256"], code)
        except (AgentError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            if isinstance(exc, AgentError) and exc.code == "MOBILE_MAX_READBACK_UNSUPPORTED":
                raise
            raise AgentError(code) from exc
        return journal, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _max_journal_identity(journal):
        return fingerprint([journal[key] for key in ("started_at", "before_count", "content_hash")])

    def _reconcile_max(self, plan, snapshot, journal_identity, actor):
        dispatch_id = plan["dispatch_id"]
        frozen = {k: v for k, v in plan.items() if k not in {"state", "owner", "lease_until"}}
        journal, _ = self._max_journal(frozen, snapshot)
        _require(self._max_journal_identity(journal) == journal_identity, "MOBILE_PLAN_CHANGED")
        self.core._expire_send_leases(dispatch_id=dispatch_id, actor=actor)
        current, _ = self._reconcile_plan(dispatch_id)
        _require(current == {**frozen, "state": "unknown", "owner": None, "lease_until": 0},
                 "MOBILE_DISPATCH_LEASE_ACTIVE")
        journal, _ = self._max_journal(frozen, snapshot)
        _require(self._max_journal_identity(journal) == journal_identity, "MOBILE_PLAN_CHANGED")
        observed = self.core.publisher.call("reconcile", chat_id=frozen["destination"], request_id=dispatch_id)
        _require(isinstance(observed, dict), "MOBILE_MAX_READBACK_PROOF_INVALID")
        if observed.get("state") == "unknown":
            return {"dispatch_id": dispatch_id, "dispatch_state": "unknown", "verified": False,
                    "outcome": "proof_insufficient", "evidence_kind": "unverified", "external_mutations": 0}
        journal, proof_hash = self._max_journal(frozen, snapshot)
        _require(self._max_journal_identity(journal) == journal_identity
                 and journal.get("state") == "confirmed" and journal.get("delivery") == observed
                 and observed.get("external_id") == "browser-" + dispatch_id,
                 "MOBILE_MAX_READBACK_PROOF_INVALID")
        if journal["presentation"]:
            proof = observed.get("image_proof")
            _require(isinstance(proof, dict), "MOBILE_MAX_READBACK_PROOF_INVALID")
            title = proof.get("title_proof")
            _require(isinstance(title, dict)
                     and proof.get("contract") == journal["presentation"]["presentation_contract"]
                     and proof.get("illustration_proof_hash") == journal["presentation"]["illustration_proof_hash"]
                     and proof.get("uploaded_sha256") == journal["illustration_image_sha256"]
                     and proof.get("image_before_caption") is True and title.get("title_bold") is True
                     and title.get("body_unbold") is True and title.get("title_text") == snapshot["title"],
                     "MOBILE_MAX_READBACK_PROOF_INVALID")
        with self.core.db.session.begin() as session:
            variant = session.scalar(select(Variant).where(Variant.id == frozen["variant_id"]).with_for_update())
            target = session.scalar(select(Target).where(Target.id == frozen["target_id"]).with_for_update())
            dispatch = session.scalar(select(Dispatch).where(Dispatch.id == dispatch_id).with_for_update())
            _require(variant is not None and target is not None and dispatch is not None
                     and variant.target_id == target.id and dispatch.variant_id == variant.id
                     and dispatch.state == "unknown" and dispatch.owner is None and dispatch.lease_until == 0,
                     "MOBILE_PLAN_CHANGED")
            _require(self._dispatch_fence(session, dispatch, variant, target, lock=True) == frozen,
                     "MOBILE_PLAN_CHANGED")
            _require(self._max_journal(frozen, snapshot)[1] == proof_hash, "MOBILE_PLAN_CHANGED")
            observed_at = now()
            dispatch.state, dispatch.verified, dispatch.error = "published", True, None
            dispatch.external_id, dispatch.permalink = observed["external_id"], _remote_link(observed.get("permalink"))
            variant.state = "published"
            attempt = DispatchAttempt(dispatch_id=dispatch.id, result="published", created_at=observed_at)
            session.add(attempt)
            self.core.db.audit(session, actor, "route_reconcile_read_only", dispatch.id,
                               snapshot_hash=dispatch.snapshot_hash, verified=True, result="published",
                               evidence={"fixture": False, "verification": "browser_readback",
                                         "independent_readback": True, "journal_sha256": proof_hash,
                                         "observed_at": observed_at})
            if snapshot.get("metadata", {}).get("origin") == "hourly_article_test":
                target.paused = True
                session.flush()
                self.core._record_reserve_delivery(session, dispatch, attempt)
        return {"dispatch_id": dispatch_id, "dispatch_state": "published", "verified": True,
                "outcome": "delivery_confirmed", "evidence_kind": "independent_readback",
                "observed_at": _timestamp(observed_at), "external_mutations": 0}

    def retirement_receipt(self, record_id, operation_id, *, actor):
        from .preapproval_retirement import operation_status

        try:
            result = operation_status(self.core, operation_id, expected_actor=actor,
                                      expected_record_id=record_id)
        except AgentError:
            return None
        if result["state"] != "retired_no_send":
            return None
        return self._retirement_result(record_id, result)

    @staticmethod
    def _retirement_result(record_id, result):
        return {"record_id": record_id, "state": result["state"], "slot_state": "missed",
                "outcome": "retired_no_send",
                "draft_preserved": True, "postiz_id": result["postiz_id"],
                "slot_id": result["slot_id"], "external_mutations": 0}

    def _retire(self, record_id, plan_hash, actor, operation_id, reason):
        from .preapproval_retirement import apply, operation_status

        try:
            result = apply(self.core, record_id, operation_id=operation_id, plan_hash=plan_hash,
                           actor=actor, reason=reason, retire_confirmed=True)
        except AgentError:
            # A durable claim can outlive a failed proof read. Do not imply
            # that a retry under a new operation ID is safe.
            try:
                status = operation_status(self.core, operation_id)
            except AgentError as error:
                if error.code != "PREAPPROVAL_OPERATION_NOT_FOUND":
                    raise AgentError("MOBILE_OPERATION_UNKNOWN", 503) from error
                status = None
            if status is not None:
                committed = self.retirement_receipt(record_id, operation_id, actor=actor)
                if committed is not None:
                    return committed
                raise AgentError("MOBILE_OPERATION_UNKNOWN", 503) from None
            raise
        return self._retirement_result(record_id, result)
