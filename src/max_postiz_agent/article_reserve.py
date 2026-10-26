"""Долговечные claims резерва и назначения доставки без сетевых операций.

Каждый claim живёт до известного завершения. Таймаут, рестарт и потеря владельца
не освобождают модели или адресата и не дают права повторить внешний запрос.
"""
import copy
import math
import re
from collections import Counter
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import AgentError, fingerprint
from .db import now, uid
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
from .server_models import EditorialControl, EditorialLedger, EditorialPreparationClaim, PublicationSlot
from .server_state import CONTROL_ID, LEDGER_ID, require_running

RESERVE_CONTRACT = "article-reserve-v1"
ACTIVE_SLOT_STATES = {"assigned", "pending", "blocked_unknown", "delivered"}
_IDENTITY = ("id", "target_id", "source_id", "url", "original_hash", "input_hash", "policy_hash",
             "source_published_at", "variant_id", "reserve_contract", "reserve_prepared_hash")
_TRANSITIONS = {
    "assigned": {"assigned", "pending", "blocked_unknown", "delivered"},
    "pending": {"pending", "blocked_unknown", "delivered", "published"},
    "blocked_unknown": {"blocked_unknown", "delivered", "published"},
    "delivered": {"delivered", "published"},
    "published": {"published"},
    "missed": {"missed"},
}


def _claim_view(row):
    return {key: getattr(row, key) for key in (
        "id", "token", "control_revision", "record_id", "state", "error", "created_at", "updated_at")}


def _slot_view(row, *, private=True):
    keys = ("id", "target_id", "slot_at", "record_id", "state", "due_at", "dispatch_id",
            "delivered_at", "error", "created_at", "updated_at", "text_hash", "evidence_hash")
    result = {key: getattr(row, key) for key in keys}
    if private:
        result.update(token=row.token, control_revision=row.control_revision, binding=copy.deepcopy(row.binding))
    return result


def _stamp(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class ArticleReserveStore:
    def __init__(self, core):
        self.core = core

    def _control(self, session, *, running=True):
        if running:
            require_running(self.core, session, lock=True)
        # Уже подтверждённая доставка — факт, а не разрешение отправки. Её
        # транзакция держит Variant/Target и не берёт Control в обратном порядке.
        row = (session.scalar(select(EditorialControl).where(
            EditorialControl.id == CONTROL_ID).with_for_update()) if running
            else session.get(EditorialControl, CONTROL_ID))
        if row is None:
            raise AgentError("EDITORIAL_NOT_INITIALIZED", 409)
        return row

    def begin_preparation(self, expected_revision=None):
        """Сначала записать claim, только затем выбирать источник и вызывать модели."""
        try:
            with self.core.db.session.begin() as session:
                control = self._control(session)
                if expected_revision is not None and (
                        type(expected_revision) is not int or control.revision != expected_revision):
                    raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
                if session.scalar(select(EditorialPreparationClaim).where(
                        EditorialPreparationClaim.active_key == "models")):
                    raise AgentError("ARTICLE_RESERVE_PREPARATION_ACTIVE", 409)
                row = EditorialPreparationClaim(id=uid(), token=uid(), control_revision=control.revision,
                    state="selecting", active_key="models")
                session.add(row)
                session.flush()
                self.core.db.audit(session, "article_reserve", "reserve_preparation_claim", row.id,
                    control_revision=control.revision, contract=RESERVE_CONTRACT)
                result = _claim_view(row)
            return result
        except IntegrityError as exc:
            raise AgentError("ARTICLE_RESERVE_PREPARATION_ACTIVE", 409) from exc

    def _preparation(self, session, claim, record_id=None, *, active=True):
        control = self._control(session)
        if (not isinstance(claim, dict)
                or not all(isinstance(claim.get(k), str) and claim[k] for k in ("id", "token"))):
            raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
        row = session.scalar(select(EditorialPreparationClaim).where(
            EditorialPreparationClaim.id == claim["id"]).with_for_update())
        if (row is None or row.token != claim["token"]
                or row.control_revision != claim.get("control_revision")
                or record_id is not None and row.record_id != record_id
                or active and (row.active_key != "models" or row.state not in {"selecting", "preparing"}
                               or row.control_revision != control.revision)
                or not active and (row.state not in {"quality_ready", "rejected"} or row.active_key is not None)):
            raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
        return row

    def require_preparation(self, claim, record_id=None, *, active=True, session=None):
        if session is None:
            with self.core.db.session.begin() as own:
                return self.require_preparation(claim, record_id, active=active, session=own)
        return _claim_view(self._preparation(session, claim, record_id, active=active))

    def bind_preparation(self, claim, record_id):
        if not isinstance(record_id, str) or not record_id:
            raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
        with self.core.db.session.begin() as session:
            row = self._preparation(session, claim)
            if row.record_id not in {None, record_id}:
                raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
            row.record_id, row.state, row.updated_at = record_id, "preparing", now()
            self.core.db.audit(session, "article_reserve", "reserve_preparation_bound", row.id,
                record_id=record_id, control_revision=row.control_revision)
            return _claim_view(row)

    def finish_preparation(self, claim, state, error=None):
        if state not in {"quality_ready", "rejected", "no_source", "blocked_unknown", "failed"}:
            raise AgentError("ARTICLE_RESERVE_CLAIM_STATE_INVALID", 409)
        with self.core.db.session.begin() as session:
            row = self._preparation(session, claim)
            if state in {"quality_ready", "rejected"} and row.record_id is None:
                raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
            row.state, row.error, row.updated_at = state, error, now()
            # Инфраструктурная ошибка и неизвестный ответ не освобождают backend.
            if state in {"quality_ready", "rejected", "no_source"}:
                row.active_key = None
            self.core.db.audit(session, "article_reserve", "reserve_preparation_finished", row.id,
                record_id=row.record_id, state=state, error=error)
            return _claim_view(row)

    def active_preparation(self):
        with self.core.db.session() as session:
            row = session.scalar(select(EditorialPreparationClaim).where(
                EditorialPreparationClaim.active_key == "models"))
            return _claim_view(row) if row else None

    def _record(self, session, record_id):
        ledger = session.get(EditorialLedger, LEDGER_ID)
        matches = [r for r in ledger.payload.get("records", []) if r.get("id") == record_id] if ledger else []
        if len(matches) != 1:
            raise AgentError("ARTICLE_RESERVE_RECORD_MISMATCH", 409)
        return matches[0]

    def assign(self, record, due_at, *, text_hash, evidence_hash, min_gap_seconds=3600):
        """Назначить один готовый кандидат; никакой вызов Postiz здесь не выполняется."""
        if (not isinstance(record, dict) or record.get("state") != "quality_ready"
                or record.get("reserve_contract") != RESERVE_CONTRACT
                or not _stamp(due_at) or due_at < now()
                or type(min_gap_seconds) is not int or min_gap_seconds < 3600
                or not all(_hash(x) for x in (text_hash, evidence_hash, record.get("prepared_hash")))
                or text_hash != record.get("reserve_prepared_hash") or text_hash != record.get("prepared_hash")):
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_INVALID", 409)
        try:
            with self.core.db.session.begin() as session:
                control = self._control(session)
                target = session.scalar(select(Target).where(Target.id == record.get("target_id")).with_for_update())
                if target is None or not target.enabled:
                    raise AgentError("ARTICLE_TARGET_INVALID", 409)
                saved = self._record(session, record.get("id"))
                if fingerprint(saved) != fingerprint(record):
                    raise AgentError("ARTICLE_RESERVE_RECORD_MISMATCH", 409)
                self._preparation(session, record.get("reserve_claim"), record["id"], active=False)
                active = session.scalar(select(PublicationSlot).where(PublicationSlot.active_target == target.id))
                external = session.scalar(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, Dispatch.state.in_(ACTIVE)))
                owned = session.scalar(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id,
                    Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
                remote_unknown = session.scalar(select(RemoteDraft).join(Variant).where(
                    Variant.target_id == target.id, RemoteDraft.state.in_({"creating", "unknown"})))
                lease = session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                    BridgePost.mutation_owner.is_not(None) | (BridgePost.mutation_until != 0)
                    | BridgePost.mutation_until.is_(None)))
                if active or external or owned or remote_unknown or lease:
                    raise AgentError("ARTICLE_RESERVE_TARGET_BUSY", 409)
                interval = max(3600, min_gap_seconds, control.settings.get("interval_seconds", 3600))
                times = list(session.scalars(select(PublicationSlot.delivered_at).where(
                    PublicationSlot.target_id == target.id, PublicationSlot.delivered_at.is_not(None))))
                attempts = list(session.scalars(select(DispatchAttempt).join(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, DispatchAttempt.result == "published")))
                known = {attempt.dispatch_id for attempt in attempts}
                published = list(session.scalars(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, Dispatch.state == "published")))
                if any(dispatch.id not in known for dispatch in published):
                    raise AgentError("ARTICLE_PUBLISH_TIME_UNKNOWN", 409)
                times.extend(attempt.created_at for attempt in attempts)
                if times and due_at < max(times) + interval + 1:
                    raise AgentError("ARTICLE_HOURLY_GAP", 409)
                if (session.scalar(select(TargetApproval).where(TargetApproval.variant_id == record.get("variant_id")))
                        or session.scalar(select(Dispatch).where(Dispatch.variant_id == record.get("variant_id")))):
                    raise AgentError("ARTICLE_RESERVE_ALREADY_ASSIGNED", 409)
                slot_at = int(due_at // 3600) * 3600
                slot = PublicationSlot(id=str(uuid5(NAMESPACE_URL,
                    f"max-postiz:publication-slot:{target.id}:{slot_at}")), token=uid(), target_id=target.id,
                    slot_at=slot_at, record_id=record["id"], text_hash=text_hash,
                    evidence_hash=evidence_hash, binding={key: saved.get(key) for key in _IDENTITY},
                    control_revision=control.revision, active_target=target.id, due_at=due_at, state="assigned")
                session.add(slot)
                session.flush()
                self.core.db.audit(session, "article_reserve", "reserve_slot_assigned", slot.id,
                    target_id=target.id, record_id=record["id"], due_at=due_at,
                    text_hash=text_hash, evidence_hash=evidence_hash,
                    binding_hash=fingerprint(slot.binding), control_revision=control.revision)
                result = _slot_view(slot)
            return result
        except IntegrityError as exc:
            raise AgentError("ARTICLE_RESERVE_ALREADY_ASSIGNED", 409) from exc

    def require_assignment(self, record_id, *, slot_id=None, session=None):
        if session is None:
            with self.core.db.session.begin() as own:
                return self.require_assignment(record_id, slot_id=slot_id, session=own)
        control = self._control(session)
        query = select(PublicationSlot).where(PublicationSlot.record_id == record_id)
        if slot_id is not None:
            query = query.where(PublicationSlot.id == slot_id)
        rows = list(session.scalars(query))
        if len(rows) != 1:
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_LOST", 409)
        row = rows[0]
        record = self._record(session, record_id)
        if (row.state not in {"assigned", "pending"} or row.active_target != row.target_id
                or row.delivered_at is not None or row.control_revision != control.revision
                or row.binding != {key: record.get(key) for key in _IDENTITY}):
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_LOST", 409)
        return _slot_view(row)

    def rebind_pending(self, session, record_id, dispatch_id):
        """После полной GET-сверки вернуть fence прежнему QUEUE, без нового POST.

        Вызывается только из ServerCycle._resume_pending в той же транзакции,
        после сверки удалённого точного текста, даты, approval и quality evidence.
        Само наличие старого слота не разрешает возобновление.
        """
        control = self._control(session)
        record = self._record(session, record_id)
        row = session.scalar(select(PublicationSlot).where(
            PublicationSlot.record_id == record_id).with_for_update())
        dispatch = session.get(Dispatch, dispatch_id)
        variant = session.get(Variant, record.get("variant_id"))
        approval = session.get(TargetApproval, dispatch.approval_id) if dispatch else None
        link = session.get(BridgePost, record.get("postiz_id")) if record.get("postiz_id") else None
        unsafe = (
            row is None or dispatch is None or variant is None or approval is None or link is None
            or row.state not in {"assigned", "pending"} or row.active_target != row.target_id
            or row.delivered_at is not None or row.dispatch_id not in {None, dispatch_id}
            or row.binding != {key: record.get(key) for key in _IDENTITY}
            or record.get("dispatch_id") != dispatch_id or record.get("due_at") != row.due_at
            or dispatch.variant_id != variant.id or dispatch.state != "pending"
            or dispatch.owner is not None or dispatch.lease_until != 0 or dispatch.attempts != 0
            or dispatch.verified or dispatch.error or dispatch.external_id is not None or dispatch.permalink is not None
            or dispatch.due_at != row.due_at or dispatch.revision != variant.revision
            or variant.target_id != row.target_id
            or row.text_hash != record.get("reserve_prepared_hash")
            or variant.metadata_json.get("machine_review_evidence_hash") != row.evidence_hash
            or approval.variant_id != variant.id or approval.revision != variant.revision
            or approval.revoked or approval.actor != "hourly_article_policy"
            or approval.snapshot_hash != dispatch.snapshot_hash or approval.scheduled_at != row.due_at
            or link.variant_id != variant.id or not link.active or link.error or link.remote_state != "QUEUE"
            or link.mutation_owner is not None or link.mutation_until != 0
            or session.scalar(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch_id))
        )
        if unsafe:
            raise AgentError("ARTICLE_RESERVE_PENDING_RESUME_NOT_SAFE", 409)
        if session.scalar(select(Dispatch).join(Variant).where(Variant.target_id == row.target_id,
                Dispatch.id != dispatch_id, Dispatch.state.in_(ACTIVE))):
            raise AgentError("ARTICLE_RESERVE_PENDING_RESUME_NOT_SAFE", 409)
        previous = row.control_revision
        row.control_revision, row.dispatch_id, row.state = control.revision, dispatch_id, "pending"
        row.updated_at = now()
        self.core.db.audit(session, "article_reserve", "reserve_pending_refenced", row.id,
            previous_revision=previous, control_revision=control.revision,
            dispatch_id=dispatch_id, due_at=row.due_at, snapshot_hash=dispatch.snapshot_hash)
        return _slot_view(row)

    def update(self, slot_id, state, *, token, dispatch_id=None, delivered_at=None, error=None, session=None):
        if state not in _TRANSITIONS or delivered_at is not None and not _stamp(delivered_at):
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if session is None:
            with self.core.db.session.begin() as own:
                return self.update(slot_id, state, token=token, dispatch_id=dispatch_id,
                    delivered_at=delivered_at, error=error, session=own)
        # Подтверждённый факт доставки сохраняется даже при общей паузе.
        control = self._control(session, running=state not in {"delivered", "published"})
        row = session.scalar(select(PublicationSlot).where(PublicationSlot.id == slot_id).with_for_update())
        if (row is None or row.token != token or state not in _TRANSITIONS.get(row.state, set())
                or state not in {"delivered", "published"} and row.control_revision != control.revision
                or row.dispatch_id is not None and dispatch_id not in {None, row.dispatch_id}
                or row.delivered_at is not None and delivered_at not in {None, row.delivered_at}):
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        bound_dispatch = dispatch_id or row.dispatch_id
        dispatch = session.get(Dispatch, bound_dispatch) if bound_dispatch else None
        if bound_dispatch is not None and (dispatch is None or dispatch.variant_id != row.binding.get("variant_id")):
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if state in {"pending", "delivered", "published"} and not dispatch:
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        actual_time = delivered_at if delivered_at is not None else row.delivered_at
        if state in {"delivered", "published"}:
            attempt = session.scalar(select(DispatchAttempt).where(
                DispatchAttempt.dispatch_id == bound_dispatch, DispatchAttempt.result == "published",
                DispatchAttempt.created_at == actual_time))
            if (actual_time is None or dispatch.state != "published"
                    or dispatch.verified is not True or attempt is None):
                raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if state == "published":
            record = self._record(session, row.record_id)
            if (record.get("state") != "published" or record.get("postiz_state") != "PUBLISHED"
                    or record.get("dispatch_id") != bound_dispatch
                    or record.get("published_at") != actual_time or not record.get("postiz_id")):
                raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if dispatch_id is not None:
            row.dispatch_id = dispatch_id
        if delivered_at is not None:
            row.delivered_at = delivered_at
        row.state, row.error, row.updated_at = state, error, now()
        row.active_target = row.target_id if state in ACTIVE_SLOT_STATES else None
        self.core.db.audit(session, "article_reserve", "reserve_slot_state", row.id,
            state=state, dispatch_id=row.dispatch_id, delivered_at=row.delivered_at, error=error)
        return _slot_view(row)

    def status(self, target_id=None):
        with self.core.db.session() as session:
            query = select(PublicationSlot)
            if target_id is not None:
                query = query.where(PublicationSlot.target_id == target_id)
            rows = list(session.scalars(query))
            active = [_slot_view(row, private=False) for row in rows if row.active_target]
            delivered = [row.delivered_at for row in rows if row.delivered_at is not None]
            return {"slots": dict(Counter(row.state for row in rows)), "active": active,
                "last_delivered_at": max(delivered) if delivered else None,
                "next_delivery_at": min(row["due_at"] for row in active) if active else None}

    def get(self, slot_id):
        """Чтение прежнего claim для сверки, включая паузу и неизвестный исход."""
        with self.core.db.session() as session:
            row = session.get(PublicationSlot, slot_id)
            if row is None:
                raise AgentError("ARTICLE_RESERVE_SLOT_NOT_FOUND", 404)
            return _slot_view(row)
