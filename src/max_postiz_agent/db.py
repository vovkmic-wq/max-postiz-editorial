# Модели базы, транзакции, сериализация и сохранение истории.
"""Portable SQLAlchemy state; PostgreSQL locking, single-writer SQLite development mode."""

import json
import os
import uuid
from pathlib import Path
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


# Создаём уникальный идентификатор записи.
def uid():
    return str(uuid.uuid4())


# Возвращаем время для сохранённых отметок операций.
def now():
    return datetime.now(UTC).timestamp()


# Структура Base объединяет связанные данные и операции модуля.
class Base(DeclarativeBase):
    pass


# Структура ConfigVersion объединяет связанные данные и операции модуля.
class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Профиль подключённого канала и подтверждённые возможности отправки.
class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)


# Структура Cursor объединяет связанные данные и операции модуля.
class Cursor(Base):
    __tablename__ = "source_cursors"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success: Mapped[float | None] = mapped_column(Float)
    next_due: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(64), default="not_run")


# Структура Story объединяет связанные данные и операции модуля.
class Story(Base):
    __tablename__ = "stories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    text_hash: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Структура Material объединяет связанные данные и операции модуля.
class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("source_id", "external_id", "text_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(Text)
    story_id: Mapped[str] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Черновик с ревизией, состоянием и привязкой к профилю канала.
class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("channel_id", "story_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(String(64))
    story_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(64), default="review_pending")
    current: Mapped[dict] = mapped_column(JSON)
    config_id: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[float | None] = mapped_column(Float)
    commercial: Mapped[bool] = mapped_column(Boolean, default=False)


# Структура DraftRevision объединяет связанные данные и операции модуля.
class DraftRevision(Base):
    __tablename__ = "draft_revisions"
    __table_args__ = (UniqueConstraint("draft_id", "revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    draft_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    config_id: Mapped[str] = mapped_column(String(64))


# Структура Approval объединяет связанные данные и операции модуля.
class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    draft_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    config_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# Устойчивая запись намерения отправки, защищающая от повторной публикации.
class Intent(Base):
    __tablename__ = "publication_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    draft_id: Mapped[str] = mapped_column(String(36), unique=True)
    channel_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(64), default="pending")
    external_id: Mapped[str | None] = mapped_column(String(128))
    permalink: Mapped[str | None] = mapped_column(Text)
    send_started_at: Mapped[float | None] = mapped_column(Float)
    published_at: Mapped[float | None] = mapped_column(Float)
    body_hash: Mapped[str] = mapped_column(String(64))
    retry_after: Mapped[float] = mapped_column(Float, default=0)


# Структура Attempt объединяет связанные данные и операции модуля.
class Attempt(Base):
    __tablename__ = "publication_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    intent_id: Mapped[str] = mapped_column(String(36))
    result: Mapped[str] = mapped_column(String(64))
    at: Mapped[float] = mapped_column(Float, default=now)


# Структура Job объединяет связанные данные и операции модуля.
class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(256), unique=True)
    kind: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64))
    config_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(64), default="queued")
    due_at: Mapped[float] = mapped_column(Float)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    owner: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(64))


# Структура Inbox объединяет связанные данные и операции модуля.
class Inbox(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (UniqueConstraint("platform", "event_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    platform: Mapped[str] = mapped_column(String(32))
    event_key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict] = mapped_column(JSON)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


# Структура Generation объединяет связанные данные и операции модуля.
class Generation(Base):
    __tablename__ = "generation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_type: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    runtime: Mapped[str] = mapped_column(String(32))
    config_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(64))
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Структура Budget объединяет связанные данные и операции модуля.
class Budget(Base):
    __tablename__ = "budget_ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(String(64))
    action_key: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Структура Campaign объединяет связанные данные и операции модуля.
class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="planning")


# Структура Metric объединяет связанные данные и операции модуля.
class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    origin: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Структура Audit объединяет связанные данные и операции модуля.
class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Float, default=now)


# Структура VideoProject объединяет связанные данные и операции модуля.
class VideoProject(Base):
    __tablename__ = "video_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    plan: Mapped[dict] = mapped_column(JSON)
    config_id: Mapped[str] = mapped_column(String(64))
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(64), default="planning")
    draft_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Структура VideoScene объединяет связанные данные и операции модуля.
class VideoScene(Base):
    __tablename__ = "video_scenes"
    __table_args__ = (UniqueConstraint("project_id", "index"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(36))
    index: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(36), default=uid, unique=True)
    backend_hash: Mapped[str | None] = mapped_column(String(64))
    remote_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(64), default="pending")
    artifact: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[float | None] = mapped_column(Float)


# Структура VideoAsset объединяет связанные данные и операции модуля.
class VideoAsset(Base):
    __tablename__ = "video_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    path: Mapped[str] = mapped_column(Text)
    frames: Mapped[list] = mapped_column(JSON)
    quality: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=now)


# Структура MaterialMedia объединяет связанные данные и операции модуля.
class MaterialMedia(Base):
    __tablename__ = "material_media"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    material_id: Mapped[str] = mapped_column(String(36))
    url: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(String(64))


# Управление соединением, транзакциями и переносимым снимком базы.
class Database:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, settings, url=None):
        self.root = settings.root
        state = settings.root / settings.app["state_dir"]
        state.mkdir(parents=True, exist_ok=True)
        url = (
            url
            or os.getenv(settings.app["database_url_env"])
            or ("sqlite:///" + (state / "agent.db").as_posix())
        )
        if settings.app.get("require_postgresql", False) and not url.startswith("postgresql"):
            from .config import AgentError
            raise AgentError("POSTGRESQL_REQUIRED", 503)
        args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite:") else {}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        # Обрабатываем ветвь при условии: self.engine.dialect.name == 'sqlite'.
        if self.engine.dialect.name == "sqlite":

            # Включаем режим WAL и ожидание блокировок SQLite.
            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=30000")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    # Создаём отсутствующие настройки или структуру хранения.
    def initialize(self):
        from . import hub_models  # noqa: F401 -- register hub tables before either backend
        if self.engine.dialect.name == "postgresql":
            from alembic import command
            from alembic.config import Config
            root = self.root if (self.root / "migrations").exists() else Path(__file__).resolve().parents[2]
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            with self.engine.begin() as connection:
                cfg.attributes["connection"] = connection
                command.upgrade(cfg, "head")
            return
        Base.metadata.create_all(self.engine)

    # Записываем действие и безопасные сведения о нём.
    def audit(self, session, actor, action, entity, **details):
        session.add(Audit(actor=actor, action=action, entity=entity, details=details))

    # Выгружаем согласованный снимок таблиц для резервирования.
    def export(self):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                return {
                    table.name: [dict(row._mapping) for row in connection.execute(table.select())]
                    for table in Base.metadata.sorted_tables
                }

    # Проверяем архив и восстанавливаем его только в пустую базу.
    def restore(self, data):
        # New database only. Never silently replace an existing running installation.
        from .config import AgentError

        missing = set(Base.metadata.tables) - set(data)
        # Обрабатываем ветвь при условии: set(data) - set(Base.metadata.tables) or missing - {'video_projects', 'video_scenes', 'video_assets', 'mate….
        if set(data) - set(Base.metadata.tables) or missing - {
            "video_projects",
            "video_scenes",
            "video_assets",
            "material_media",
            "hub_bridge_posts",
        }:
            raise AgentError("BACKUP_SCHEMA_MISMATCH")
        data = {name: data.get(name, []) for name in Base.metadata.tables}
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.engine.begin() as connection:
            # Последовательно обрабатываем элементы: Base.metadata.sorted_tables.
            for table in Base.metadata.sorted_tables:
                # Обрабатываем ветвь при условии: connection.execute(table.select().limit(1)).first().
                if connection.execute(table.select().limit(1)).first():
                    raise AgentError("RESTORE_REQUIRES_EMPTY_DATABASE")
            # Последовательно обрабатываем элементы: Base.metadata.sorted_tables.
            for table in Base.metadata.sorted_tables:
                # Обрабатываем ветвь при условии: data[table.name].
                if data[table.name]:
                    connection.execute(table.insert(), data[table.name])


# Удаляем поля с секретами из сохраняемого служебного объекта.
def safe_payload(value):
    """Prevent control API and webhook payloads from becoming secret storage."""
    # Обрабатываем ветвь при условии: isinstance(value, dict).
    if isinstance(value, dict):
        return {
            k: safe_payload(v)
            for k, v in value.items()
            if not any(word in k.lower() for word in ("token", "secret", "password", "cookie"))
        }
    # Обрабатываем ветвь при условии: isinstance(value, list).
    if isinstance(value, list):
        return [safe_payload(v) for v in value]
    return value


# Преобразуем запись хранения в сериализуемые данные.
def serialize(row):
    return json.loads(json.dumps({c.name: getattr(row, c.name) for c in row.__table__.columns}))
