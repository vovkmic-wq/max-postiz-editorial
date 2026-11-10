"""Durable, principal-scoped plans and operation receipts for the mobile API."""
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Float, ForeignKey, String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .config import AgentError, fingerprint
from .db import Base, now

PLAN_TTL = 120
RUNNING_TIMEOUT = 600


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def safe_code(value, default="MOBILE_OPERATION_UNCERTAIN"):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", value) else default


class MobilePlan(Base):
    __tablename__ = "mobile_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[str] = mapped_column(String(128))
    plan_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class MobileOperation(Base):
    __tablename__ = "mobile_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("mobile_plans.id"), unique=True)
    actor: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(24))
    code: Mapped[str | None] = mapped_column(String(96))
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[float] = mapped_column(Float)


class MobileJournal:
    def __init__(self, db, environment):
        self.db = db
        self.environment = environment

    def plan(self, actor, action, object_id, payload):
        plan_id = str(uuid4())
        current = now()
        expires = current + PLAN_TTL
        digest = fingerprint({"id": plan_id, "actor": actor, "environment": self.environment,
                              "action": action, "object_id": object_id, "payload": payload,
                              "expires_at": expires})
        with self.db.session.begin() as session:
            session.execute(delete(MobilePlan).where(
                MobilePlan.expires_at < current - 3600,
                ~MobilePlan.id.in_(select(MobileOperation.plan_id))))
            session.add(MobilePlan(id=plan_id, actor=actor, environment=self.environment,
                                   action=action, object_id=object_id, plan_hash=digest,
                                   payload=payload, expires_at=expires))
        return {"plan_id": plan_id, "plan_hash": digest, "action": action, "object_id": object_id,
                "effect": payload["effect"], "expires_at": timestamp(expires),
                "details": payload.get("public", {})}

    def _visible(self, row, actor):
        return row is not None and row.actor == actor and row.environment == self.environment

    def _view(self, row):
        state, code = row.state, row.code
        if state == "running" and now() - row.updated_at > RUNNING_TIMEOUT:
            state, code = "unknown", "MOBILE_OPERATION_UNCERTAIN"
        return {"operation_id": row.id, "action": row.action, "object_id": row.object_id,
                "state": state, "code": code, "result": row.result,
                "actor": row.actor, "reason": row.reason,
                "created_at": timestamp(row.created_at), "updated_at": timestamp(row.updated_at)}

    def _replay(self, session, actor, operation_id, digest):
        row = session.get(MobileOperation, operation_id)
        if row is None:
            return None
        if not self._visible(row, actor):
            raise AgentError("MOBILE_OPERATION_NOT_FOUND", 404)
        if row.request_hash != digest:
            raise AgentError("MOBILE_OPERATION_CONFLICT", 409)
        return self._view(row)

    def claim(self, actor, operation_id, plan_id, plan_hash, reason, authorize):
        digest = fingerprint({"plan_id": plan_id, "plan_hash": plan_hash, "reason": reason})
        try:
            with self.db.session.begin() as session:
                replay = self._replay(session, actor, operation_id, digest)
                if replay is not None:
                    return None, replay
                plan = session.scalar(select(MobilePlan).where(MobilePlan.id == plan_id)
                                      .with_for_update())
                if not self._visible(plan, actor):
                    raise AgentError("MOBILE_PLAN_NOT_FOUND", 404)
                # Recheck after locking to handle simultaneous submissions of a plan.
                replay = self._replay(session, actor, operation_id, digest)
                if replay is not None:
                    return None, replay
                if plan.plan_hash != plan_hash or plan.expires_at <= now():
                    raise AgentError("MOBILE_PLAN_CHANGED_OR_EXPIRED", 409)
                if session.scalar(select(MobileOperation.id)
                                  .where(MobileOperation.plan_id == plan_id)) is not None:
                    raise AgentError("MOBILE_PLAN_USED", 409)
                authorize(plan.action)
                stamp = now()
                row = MobileOperation(id=operation_id, plan_id=plan_id, actor=actor,
                                      environment=self.environment, action=plan.action,
                                      object_id=plan.object_id, request_hash=digest, reason=reason,
                                      state="running",
                                      created_at=stamp, updated_at=stamp)
                session.add(row)
                return (plan.action, plan.object_id, plan.payload), self._view(row)
        except IntegrityError:
            with self.db.session() as session:
                replay = self._replay(session, actor, operation_id, digest)
                if replay is not None:
                    return None, replay
            raise AgentError("MOBILE_PLAN_USED", 409) from None

    def finish(self, operation_id, state, *, code=None, result=None):
        with self.db.session.begin() as session:
            row = session.scalar(select(MobileOperation).where(MobileOperation.id == operation_id)
                                 .with_for_update())
            if row is None or row.state != "running":
                raise AgentError("MOBILE_OPERATION_UNCERTAIN", 503)
            row.state, row.code, row.result, row.updated_at = state, code, result, now()
            return self._view(row)

    def status(self, actor, operation_id, *, plan_id=None, plan_hash=None):
        with self.db.session() as session:
            row = session.get(MobileOperation, operation_id)
            if self._visible(row, actor):
                return self._view(row)
            if row is not None or plan_id is None:
                raise AgentError("MOBILE_OPERATION_NOT_FOUND", 404)
            # Share the plan lock with claim. A 404 while a claim is still
            # committing cannot prove that a command was never accepted.
            plan = session.scalar(select(MobilePlan).where(
                MobilePlan.id == plan_id, MobilePlan.actor == actor, MobilePlan.environment == self.environment,
                MobilePlan.plan_hash == plan_hash, MobilePlan.expires_at <= now()).with_for_update())
            if plan is not None:
                row = session.get(MobileOperation, operation_id)
                if self._visible(row, actor):
                    return self._view(row)
                if row is None and session.scalar(select(MobileOperation.id).where(
                        MobileOperation.plan_id == plan.id)) is None:
                    raise AgentError("MOBILE_OPERATION_NOT_ACCEPTED", 410)
            raise AgentError("MOBILE_OPERATION_NOT_FOUND", 404)

    def audit(self, actor, before, limit):
        query = select(MobileOperation).where(MobileOperation.environment == self.environment)
        if actor is not None:
            query = query.where(MobileOperation.actor == actor)
        if before is not None:
            when, operation_id = before
            query = query.where((MobileOperation.created_at < when) |
                                ((MobileOperation.created_at == when) &
                                 (MobileOperation.id < operation_id)))
        with self.db.session() as session:
            rows = list(session.scalars(query.order_by(
                MobileOperation.created_at.desc(), MobileOperation.id.desc()).limit(limit + 1)))
            cursor = (rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
            return [self._view(row) for row in rows[:limit]], cursor
