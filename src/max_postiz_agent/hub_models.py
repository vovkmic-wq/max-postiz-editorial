"""Multi-platform records; the MAX editor remains the source of editorial drafts."""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now, uid


class Target(Base):
    __tablename__ = "hub_targets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    route: Mapped[str] = mapped_column(String(32))
    destination: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    options: Mapped[dict] = mapped_column(JSON, default=dict)


class Variant(Base):
    __tablename__ = "hub_variants"
    __table_args__ = (UniqueConstraint("draft_id", "target_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"))
    target_id: Mapped[str] = mapped_column(ForeignKey("hub_targets.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text)
    media: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_revision: Mapped[int] = mapped_column(Integer)
    config_hash: Mapped[str] = mapped_column(String(64))
    binding_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="review_pending")
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class VariantRevision(Base):
    __tablename__ = "hub_variant_revisions"
    __table_args__ = (UniqueConstraint("variant_id", "revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    variant_id: Mapped[str] = mapped_column(ForeignKey("hub_variants.id"))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=now)


class TargetApproval(Base):
    __tablename__ = "hub_approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    variant_id: Mapped[str] = mapped_column(ForeignKey("hub_variants.id"))
    revision: Mapped[int] = mapped_column(Integer)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[float] = mapped_column(Float)
    actor: Mapped[str] = mapped_column(String(64))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Dispatch(Base):
    __tablename__ = "hub_dispatches"
    __table_args__ = (UniqueConstraint("variant_id", "revision", "snapshot_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    variant_id: Mapped[str] = mapped_column(ForeignKey("hub_variants.id"))
    approval_id: Mapped[str] = mapped_column(ForeignKey("hub_approvals.id"))
    revision: Mapped[int] = mapped_column(Integer)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    due_at: Mapped[float] = mapped_column(Float)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(64))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class RemoteDraft(Base):
    __tablename__ = "hub_remote_drafts"
    variant_id: Mapped[str] = mapped_column(ForeignKey("hub_variants.id"), primary_key=True)
    external_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="creating")
    remote_hash: Mapped[str | None] = mapped_column(String(64))
    synced_revision: Mapped[int] = mapped_column(Integer)
    correlation: Mapped[str] = mapped_column(String(64), unique=True)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class DispatchAttempt(Base):
    __tablename__ = "hub_dispatch_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    dispatch_id: Mapped[str] = mapped_column(ForeignKey("hub_dispatches.id"))
    result: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=now)


class SourceArchive(Base):
    __tablename__ = "hub_source_archive"
    __table_args__ = (UniqueConstraint("source_id", "response_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    response_hash: Mapped[str] = mapped_column(String(64))
    blob_path: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class RecurringTask(Base):
    __tablename__ = "hub_recurring_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="digest")
    interval_seconds: Mapped[int] = mapped_column(Integer)
    lookback_hours: Mapped[int] = mapped_column(Integer, default=24)
    due_at: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    last_slot: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(64))


class BridgePost(Base):
    __tablename__ = "hub_bridge_posts"
    post_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    integration_id: Mapped[str] = mapped_column(String(64))
    group_id: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(ForeignKey("hub_targets.id"))
    variant_id: Mapped[str] = mapped_column(ForeignKey("hub_variants.id"), unique=True)
    remote_hash: Mapped[str] = mapped_column(String(64))
    remote_state: Mapped[str] = mapped_column(String(32))
    due_at: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(64))
    mutation_owner: Mapped[str | None] = mapped_column(String(36))
    mutation_until: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class RedditSubmission(Base):
    __tablename__ = "hub_reddit_submissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    reddit_id: Mapped[str] = mapped_column(String(32), unique=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    original: Mapped[str] = mapped_column(Text, default="")
    original_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    origin: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(ForeignKey("hub_targets.id"))
    state: Mapped[str] = mapped_column(String(32), default="awaiting_rights", index=True)
    rights_ref: Mapped[str] = mapped_column(Text, default="")
    rights_actor: Mapped[str] = mapped_column(String(64), default="")
    variant_id: Mapped[str | None] = mapped_column(ForeignKey("hub_variants.id"))
    translation_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
