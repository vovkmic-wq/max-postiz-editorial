"""Постоянное состояние автономной редакции; старые таблицы истории сохраняются."""

from sqlalchemy import JSON, Boolean, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now, uid


class EditorialControl(Base):
    __tablename__ = "editorial_control"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    runtime: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    updated_by: Mapped[str] = mapped_column(String(128), default="migration")


class EditorialLedger(Base):
    __tablename__ = "editorial_article_ledger"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class PublicationSlot(Base):
    """Одно назначение точного кандидата; неизвестный исход удерживает адресата."""
    __tablename__ = "publication_slots"
    __table_args__ = (
        UniqueConstraint("target_id", "slot_at"),
        UniqueConstraint("target_id", "record_id"),
        UniqueConstraint("target_id", "text_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    target_id: Mapped[str] = mapped_column(String(64))
    slot_at: Mapped[int] = mapped_column(Integer)
    record_id: Mapped[str] = mapped_column(String(36))
    text_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    binding: Mapped[dict] = mapped_column(JSON)
    token: Mapped[str] = mapped_column(String(36), default=uid)
    control_revision: Mapped[int] = mapped_column(Integer)
    # NULL не резервирует адресата. UNIQUE переносим между SQLite и PostgreSQL.
    active_target: Mapped[str | None] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="assigned")
    due_at: Mapped[float] = mapped_column(Float)
    dispatch_id: Mapped[str | None] = mapped_column(String(36))
    delivered_at: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class EditorialPreparationClaim(Base):
    """Запись до GET/модели не позволяет повторить неизвестную подготовку."""
    __tablename__ = "editorial_preparation_claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    token: Mapped[str] = mapped_column(String(36), default=uid)
    control_revision: Mapped[int] = mapped_column(Integer)
    record_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    active_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="selecting")
    error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
