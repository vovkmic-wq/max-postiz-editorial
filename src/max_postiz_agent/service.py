# Очередь редакции, бюджет, согласование и журнал отправок.
"""Persistent collection, editorial workflow, budget reservations and publication outbox."""

import difflib
import re
import threading
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .adapters import MaxAPI, OpenCode
from .collectors import collect_web, detect_language, messenger_items
from .config import AgentError, fingerprint
from .db import (
    Approval,
    Attempt,
    Budget,
    Campaign,
    Channel,
    ConfigVersion,
    Cursor,
    Draft,
    DraftRevision,
    Generation,
    Inbox,
    Intent,
    Job,
    Material,
    MaterialMedia,
    Metric,
    Story,
    VideoProject,
    now,
    safe_payload,
    serialize,
)
from .editorial import next_slot, numbers, translation_gate, validate_draft
from .network import SafeHTTP, validate_url


# Сервис редакции: сбор, согласование, бюджет и очередь отправки.
class Service:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, settings, db, editor=None, publisher=None, http=None):
        self.settings, self.db = settings, db
        self.editor = editor or OpenCode(settings)
        self.publisher = publisher or self.make_publisher(settings)
        self.http = http or SafeHTTP(
            settings.app["collection"]["max_response_bytes"],
            settings.app["collection"]["max_redirects"],
        )
        self.worker_id = str(uuid.uuid4())
        self.config_error = None
        from .hybrid import Hybrid

        self.hybrid = Hybrid(self)

    # Выбираем адаптер отправки по конфигурации установки.
    @staticmethod
    def make_publisher(settings):
        # Обрабатываем ветвь при условии: settings.app['max']['adapter_mode'] == 'browser'.
        if settings.app["max"]["adapter_mode"] == "browser":
            from .browser_publisher import BrowserMax

            return BrowserMax(settings)
        return MaxAPI(settings)

    # Применяем настройки и отзываем согласования устаревших черновиков.
    def activate(self, settings=None, actor="owner"):
        settings = settings or self.settings
        publisher_changed = self.settings.app["max"] != settings.app["max"]
        editor_changed = self.settings.app["opencode"] != settings.app["opencode"]
        video_changed = self.settings.app.get("video") != settings.app.get("video")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            # Обрабатываем ветвь при условии: not session.get(ConfigVersion, settings.hash).
            if not session.get(ConfigVersion, settings.hash):
                session.add(
                    ConfigVersion(
                        id=settings.hash,
                        snapshot={"topic": settings.topic, "sources": settings.sources},
                    )
                )
            # Последовательно обрабатываем элементы: settings.topic['channels'].
            for profile in settings.topic["channels"]:
                row = session.get(Channel, profile["id"])
                # Обрабатываем ветвь при условии: row and publisher_changed.
                if row and publisher_changed:
                    row.capabilities = {}
                # Обрабатываем ветвь при условии: not row.
                if not row:
                    session.add(Channel(id=profile["id"]))
                # Обрабатываем ветвь при условии: not profile['enabled'].
                elif not profile["enabled"]:
                    row.paused = True
            # Последовательно обрабатываем элементы: settings.sources['sources'].
            for source in settings.sources["sources"]:
                # Обрабатываем ветвь при условии: not session.get(Cursor, source['id']).
                if not session.get(Cursor, source["id"]):
                    session.add(Cursor(id=source["id"]))
            # Обрабатываем ветвь при условии: self.settings.hash != settings.hash or self.publisher.mode == 'browser'.
            if self.settings.hash != settings.hash or self.publisher.mode == "browser":
                # Последовательно обрабатываем элементы: session.scalars(select(Draft).where(Draft.state != 'published', Draft.config_id != settings.hash)).
                for draft in session.scalars(select(Draft).where(
                    Draft.state.not_in(["published", "archived"]), Draft.config_id != settings.hash
                )):
                    draft.state = "stale"
                session.execute(update(Approval).where(
                    Approval.config_id != settings.hash
                ).values(revoked=True))
            self.db.audit(session, actor, "activate", settings.hash)
        self.settings = settings
        # Обрабатываем ветвь при условии: publisher_changed.
        if publisher_changed:
            self.publisher.close()
            self.publisher = self.make_publisher(settings)
        # Изменённые сервисы перечитывают настройки, а не продолжают работать со старым объектом.
        if editor_changed:
            self.editor.client.close()
            self.editor = OpenCode(settings)
        if video_changed:
            from .hybrid import Hybrid
            self.hybrid.close()
            self.hybrid = Hybrid(self)
        self.config_error = None

    # Перечитываем конфигурацию или обновляем доступное состояние.
    def refresh(self):
        from .config import Settings

        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            candidate = Settings.load(self.settings.root)
            # Обрабатываем ветвь при условии: candidate.hash != self.settings.hash.
            if candidate.hash != self.settings.hash:
                self.config_error = "CONFIG_PENDING_ACTIVATION"
        # Обрабатываем (AgentError, OSError) и выбираем допустимый исход.
        except (AgentError, OSError):
            self.config_error = "CONFIG_INVALID"
        return self.config_error

    # Проверяем права адресата и сохраняем подключение профиля.
    def connect(self, channel_id, chat_id, actor="owner"):
        self.settings.channel(channel_id)
        # Обрабатываем ветвь при условии: not re.fullmatch('-?\\d+', chat_id).
        if not re.fullmatch(r"-?\d+", chat_id):
            raise AgentError("INVALID_CHAT_ID")
        caps = self.publisher.inspect(chat_id)
        # Обрабатываем ветвь при условии: not caps['publish'].
        if not caps["publish"]:
            raise AgentError("MAX_PUBLISH_FORBIDDEN")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            channel = session.get(Channel, channel_id)
            channel.chat_id, channel.capabilities = chat_id, caps
            self.db.audit(session, actor, "connect", channel_id)
        return caps

    # Изменяем паузу конкретного канала с записью в журнал.
    def pause(self, channel_id, paused=True, actor="owner"):
        profile = self.settings.channel(channel_id)
        # Обрабатываем ветвь при условии: not paused and (not profile['enabled'] or self.config_error).
        if not paused and (not profile["enabled"] or self.config_error):
            raise AgentError("CHANNEL_NOT_READY")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            row = session.get(Channel, channel_id)
            row.paused = paused
            self.db.audit(session, actor, "pause" if paused else "resume", channel_id)

    # Передаём модели ограниченную задачу с проверкой ответа.
    def model_task(self, task_type, channel_id, context):
        channel = self.settings.channel(channel_id)
        options = self.settings.app["opencode"]
        free = self.editor.mode == "fake" or options.get("local_zero_cost", False)
        quote = 0 if free else options.get("max_task_cost_rub")
        # Обрабатываем ветвь при условии: quote is None or quote < 0.
        if quote is None or quote < 0:
            raise AgentError("MODEL_PRICE_UNKNOWN")
        action_key = str(uuid.uuid4())
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            # Lock the channel for reservations across workers on PostgreSQL.
            # Обрабатываем ветвь при условии: self.db.engine.dialect.name == 'sqlite'.
            if self.db.engine.dialect.name == "sqlite":
                session.execute(
                    update(Channel).where(Channel.id == channel_id).values(paused=Channel.paused)
                )
            session.scalar(select(Channel).where(Channel.id == channel_id).with_for_update())
            day = (
                datetime.now(ZoneInfo(channel["publication"]["timezone"]))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp()
            )
            used = session.scalar(
                select(func.coalesce(func.sum(Budget.amount), 0)).where(
                    Budget.channel_id == channel_id,
                    Budget.created_at >= day,
                    Budget.state.in_(["reserved", "charged"]),
                )
            )
            # Обрабатываем ветвь при условии: used + quote > channel['budget']['daily_llm_limit'].
            if used + quote > channel["budget"]["daily_llm_limit"]:
                raise AgentError("BUDGET_EXCEEDED")
            session.add(Budget(channel_id=channel_id, action_key=action_key, amount=quote))
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            result, usage = self.editor.run(task_type, context)
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception as exc:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                session.add(
                    Generation(
                        task_type=task_type,
                        model=self.editor.model or "fake",
                        runtime=options["required_version"],
                        config_id=self.settings.hash,
                        state="failed",
                    )
                )
            # Обрабатываем ветвь при условии: isinstance(exc, AgentError).
            if isinstance(exc, AgentError):
                raise
            raise AgentError("EDITOR_TASK_FAILED") from exc
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            # No usage pricing is exposed by this V2 endpoint: retain paid reservation.
            reservation = session.scalar(select(Budget).where(Budget.action_key == action_key))
            # Обрабатываем ветвь при условии: free.
            if free:
                reservation.state = "charged"
            session.add(
                Generation(
                    task_type=task_type,
                    model=self.editor.model or "fake",
                    runtime=options["required_version"],
                    config_id=self.settings.hash,
                    state="completed",
                    usage=safe_payload(usage),
                )
            )
        return result

    # Сохраняем материал и связываем его с историей обработки.
    def ingest(self, source_id, items, cursor=None):
        source = self.settings.source(source_id)
        # Обрабатываем ветвь при условии: not source['enabled'] or not source['rights']['verified'].
        if not source["enabled"] or not source["rights"]["verified"]:
            raise AgentError("SOURCE_DISABLED")
        accepted = 0
        semantic_matches = {}
        # Обрабатываем ветвь при условии: self.editor.mode == 'live'.
        if self.editor.mode == "live":
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session() as session:
                candidates = session.scalars(
                    select(Story)
                    .where(
                        Story.created_at
                        >= now()
                        - self.settings.app["deduplication"]["semantic_window_days"] * 86400
                    )
                    .order_by(Story.created_at.desc())
                    .limit(20)
                ).all()
            # Последовательно обрабатываем элементы: items[:source['max_items_per_run']].
            for item in items[: source["max_items_per_run"]]:
                normalized = " ".join(item.text.casefold().split())
                text_hash = fingerprint(normalized)
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with self.db.session() as session:
                    # Обрабатываем ветвь при условии: session.scalar(select(Story).where(Story.text_hash == text_hash)).
                    if session.scalar(select(Story).where(Story.text_hash == text_hash)):
                        continue
                # Последовательно обрабатываем элементы: candidates.
                for candidate in candidates:
                    score = difflib.SequenceMatcher(
                        None, normalized, candidate.text.casefold()
                    ).ratio()
                    common_numbers = numbers(candidate.text) & numbers(item.text)
                    different_language = detect_language(candidate.text) != detect_language(
                        item.text
                    )
                    # Обрабатываем ветвь при условии: score < 0.6 and (not (different_language and common_numbers)).
                    if score < 0.6 and not (different_language and common_numbers):
                        continue
                    result = self.model_task(
                        "cluster",
                        source["channel_ids"][0],
                        {"candidate": candidate.text, "material": item.text},
                    )
                    # Обрабатываем ветвь при условии: result.get('same_event') is True.
                    if result.get("same_event") is True:
                        semantic_matches[text_hash] = candidate.id
                        break
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            # Последовательно обрабатываем элементы: items[:source['max_items_per_run']].
            for item in items[: source["max_items_per_run"]]:
                # Обрабатываем ветвь при условии: not item.text.strip() or not item.external_id.
                if not item.text.strip() or not item.external_id:
                    continue
                # Обрабатываем ветвь при условии: item.url.
                if item.url:
                    validate_url(item.url, resolve=False)
                normalized = " ".join(item.text.casefold().split())
                text_hash = fingerprint(normalized)
                existing = session.scalar(
                    select(Material).where(
                        Material.source_id == source_id,
                        Material.external_id == item.external_id,
                        Material.text_hash == text_hash,
                    )
                )
                # Обрабатываем ветвь при условии: existing.
                if existing:
                    continue
                story = session.scalar(select(Story).where(Story.text_hash == text_hash))
                # Обрабатываем ветвь при условии: not story and text_hash in semantic_matches.
                if not story and text_hash in semantic_matches:
                    story = session.get(Story, semantic_matches[text_hash])
                # Обрабатываем ветвь при условии: not story.
                if not story:
                    story = Story(text_hash=text_hash, title=item.title, text=item.text)
                    session.add(story)
                    session.flush()
                material = Material(
                    source_id=source_id,
                    external_id=item.external_id,
                    story_id=story.id,
                    title=item.title,
                    text=item.text,
                    url=item.url,
                    text_hash=text_hash,
                    published_at=item.published_at,
                    language=(
                        detect_language(item.text) if item.language == "auto" else item.language
                    ),
                )
                session.add(material)
                session.flush()
                # Обрабатываем ветвь при условии: source['rights'].get('allow_media').
                if source["rights"].get("allow_media"):
                    # Последовательно обрабатываем элементы: item.media[:5].
                    for media in item.media[:5]:
                        validate_url(media["url"], resolve=False)
                        session.add(
                            MaterialMedia(
                                material_id=material.id, url=media["url"], mime=media["mime"]
                            )
                        )
                # Последовательно обрабатываем элементы: source['channel_ids'].
                for channel_id in source["channel_ids"]:
                    # Обрабатываем ветвь при условии: self.settings.channel(channel_id)['enabled'].
                    if self.settings.channel(channel_id)["enabled"]:
                        self.enqueue(
                            session,
                            "edit",
                            story.id,
                            now(),
                            f"edit:{channel_id}:{story.id}:{self.settings.hash}",
                            channel_id,
                        )
                accepted += 1
            row = session.get(Cursor, source_id)
            # Обрабатываем ветвь при условии: cursor is not None.
            if cursor is not None:
                row.data = cursor
            row.last_success, row.next_due, row.status = (
                now(),
                now() + source["interval_seconds"],
                "ok",
            )
        return accepted

    # Собираем материалы источника с учётом курсора и ограничений.
    def collect(self, source_id):
        source = self.settings.source(source_id)
        # Обрабатываем ветвь при условии: not source['enabled'].
        if not source["enabled"]:
            raise AgentError("SOURCE_DISABLED")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            cursor = dict(session.get(Cursor, source_id).data)
        # Обрабатываем ветвь при условии: source['type'] in {'rss', 'html'}.
        if source["type"] in {"rss", "html", "json_api"}:
            items, next_cursor = collect_web(source, cursor, self.http, self.settings.root)
            return self.ingest(source_id, items, next_cursor)
        # Обрабатываем ветвь при условии: source['type'] == 'max_bot'.
        if source["type"] == "max_bot":
            caps = self.publisher.inspect(source["external_chat_id"])
            # Обрабатываем ветвь при условии: not caps['read'].
            if not caps["read"]:
                raise AgentError("MAX_READ_FORBIDDEN")
            messages = self.publisher.messages(
                source["external_chat_id"], count=source["max_items_per_run"]
            )
            items = []
            # Последовательно обрабатываем элементы: messages.
            for message in messages:
                items.extend(messenger_items("max", {"message": message}, source))
            return self.ingest(source_id, items)
        # Обрабатываем ветвь при условии: source['type'] == 'telegram_bot'.
        if source["type"] == "telegram_bot":
            return self.process_inbox()
        raise AgentError("SOURCE_CAPABILITY_UNSUPPORTED")

    # Добавляем задание в устойчивую очередь.
    def enqueue(self, session, kind, subject, due, key, channel_id=None):
        # Обрабатываем ветвь при условии: session.scalar(select(Job).where(Job.key == key)).
        if session.scalar(select(Job).where(Job.key == key)):
            return
        session.add(
            Job(kind=kind, subject_id=subject, due_at=due, key=key, config_id=self.settings.hash)
        )

    # Назначаем сбор источников без повторного активного задания.
    def schedule_sources(self):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            # Последовательно обрабатываем элементы: self.settings.sources['sources'].
            for source in self.settings.sources["sources"]:
                cursor = session.get(Cursor, source["id"])
                # Обрабатываем ветвь при условии: not source['enabled'] or cursor.next_due > now().
                if not source["enabled"] or cursor.next_due > now():
                    continue
                active = session.scalar(
                    select(Job).where(
                        Job.kind == "collect",
                        Job.subject_id == source["id"],
                        Job.state.in_(["queued", "leased", "retry_wait"]),
                    )
                )
                # Обрабатываем ветвь при условии: not active.
                if not active:
                    self.enqueue(
                        session,
                        "collect",
                        source["id"],
                        now(),
                        f"collect:{source['id']}:{int(cursor.next_due)}",
                    )

    # Подготавливаем редакционную версию материала.
    def edit_story(self, channel_id, story_id):
        channel = self.settings.channel(channel_id)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            existing = session.scalar(
                select(Draft).where(Draft.channel_id == channel_id, Draft.story_id == story_id)
            )
            # Обрабатываем ветвь при условии: existing.
            if existing:
                return existing.id
            materials = [
                serialize(m)
                for m in session.scalars(select(Material).where(Material.story_id == story_id))
                if channel_id in self.settings.source(m.source_id)["channel_ids"]
            ]
        # Обрабатываем ветвь при условии: not materials or not channel['enabled'].
        if not materials or not channel["enabled"]:
            raise AgentError("NO_CHANNEL_MATERIALS")
        material = materials[0]
        reason = []
        # Обрабатываем ветвь при условии: any((term.casefold() in (material['text'] + material['title']).casefold() for term in channel['exclude_topi….
        if any(
            term.casefold() in (material["text"] + material["title"]).casefold()
            for term in channel["exclude_topics"]
        ):
            reason.append("EXCLUDED_TOPIC")
        # Обрабатываем ветвь при условии: material['published_at'] is None.
        if material["published_at"] is None:
            reason.append("UNKNOWN_PUBLICATION_DATE")
        # Обрабатываем ветвь при условии: material['published_at'] < now() - channel['freshness_hours'] * 3600.
        elif material["published_at"] < now() - channel["freshness_hours"] * 3600:
            reason.append("STALE_SOURCE")
        source = self.settings.source(material["source_id"])
        # Обрабатываем ветвь при условии: not source['rights']['allow_derivative'].
        if not source["rights"]["allow_derivative"]:
            reason.append("RIGHTS_UNCLEAR")
        context = {"channel": channel, "materials": materials}
        # Обрабатываем ветвь при условии: reason.
        if reason:
            raise AgentError(reason[0])
        classification = self.model_task("classify", channel_id, context)
        relevance = classification.get("relevance")
        # Обрабатываем ветвь при условии: not isinstance(relevance, (int, float)) or relevance < channel['quality']['min_relevance'].
        if (
            not isinstance(relevance, (int, float))
            or relevance < channel["quality"]["min_relevance"]
        ):
            raise AgentError("OUT_OF_SCOPE")
        # Обрабатываем ветвь при условии: classification.get('decision') == 'reject'.
        if classification.get("decision") == "reject":
            raise AgentError("OUT_OF_SCOPE")
        # Обрабатываем ветвь при условии: material['language'] != 'ru'.
        if material["language"] != "ru":
            translated = self.model_task("translate", channel_id, context)
            translation_gate(material["text"], translated.get("text", ""))
            context["translation"] = translated["text"]
            reason.append("TRANSLATION_EDITOR_REVIEW")
        result = self.model_task("draft", channel_id, context)
        verification = self.model_task("verify", channel_id, {**context, "draft": result})
        reasons = validate_draft(result, materials, channel, verification.get("verified") is True)
        reasons.extend(reason)
        # Обрабатываем ветвь при условии: classification.get('decision') == 'review'.
        if classification.get("decision") == "review":
            reasons.append("EDITOR_REVIEW_REQUIRED")
        state = "review_pending"
        # Обрабатываем ветвь при условии: result['decision'] == 'reject'.
        if result["decision"] == "reject":
            state = "rejected"
        # Обрабатываем ветвь при условии: channel['publication']['mode'] == 'autopublish' and (not reasons) and (result['decision'] == 'accept').
        elif (
            channel["publication"]["mode"] == "autopublish"
            and not reasons
            and result["decision"] == "accept"
        ):
            state = "approved"
        result["checks"] = sorted(set(reasons))
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            draft = Draft(
                channel_id=channel_id,
                story_id=story_id,
                current=result,
                config_id=self.settings.hash,
                state=state,
            )
            session.add(draft)
            session.flush()
            session.add(
                DraftRevision(
                    draft_id=draft.id, revision=1, payload=result, config_id=self.settings.hash
                )
            )
            self.db.audit(session, "editor", "draft", draft.id, reasons=result["checks"])
            # Обрабатываем ветвь при условии: state == 'approved'.
            if state == "approved":
                self._approve(session, draft, "policy_engine")
            return draft.id

    # Сохраняем согласование после проверки хеша, версии и ограничений.
    def _approve(self, session, draft, actor):
        profile = self.settings.channel(draft.channel_id)
        # Обрабатываем ветвь при условии: self.config_error or draft.config_id != self.settings.hash.
        if self.config_error or draft.config_id != self.settings.hash:
            raise AgentError("DRAFT_CONFIG_STALE", 409)
        blocking = {
            "EXCLUDED_TOPIC",
            "OUTPUT_NOT_RUSSIAN",
            "LENGTH_EXCEEDED",
            "UNSUPPORTED_NUMBER",
            "MISSING_SOURCE_LINK",
            "INSUFFICIENT_EVIDENCE",
            "CLAIM_NOT_IN_BODY",
        }
        # Обрабатываем ветвь при условии: blocking.intersection(draft.current.get('checks', [])).
        if blocking.intersection(draft.current.get("checks", [])):
            raise AgentError("QUALITY_GATE_BLOCKED")
        # Обрабатываем ветвь при условии: draft.commercial and (not profile['monetization']['enabled']).
        if draft.commercial and not profile["monetization"]["enabled"]:
            raise AgentError("MONETIZATION_DISABLED")
        # Обрабатываем ветвь при условии: draft.commercial.
        if draft.commercial:
            recent = session.scalars(
                select(Draft).where(
                    Draft.channel_id == draft.channel_id,
                    Draft.state.in_(["scheduled", "published"]),
                    Draft.scheduled_at >= now() - 30 * 86400,
                )
            ).all()
            share = (1 + sum(d.commercial for d in recent)) / (len(recent) + 1)
            # Обрабатываем ветвь при условии: share > profile['monetization']['max_ad_share'].
            if share > profile["monetization"]["max_ad_share"]:
                raise AgentError("AD_SHARE_EXCEEDED")
        occupied = [
            d.scheduled_at
            for d in session.scalars(
                select(Draft).where(
                    Draft.channel_id == draft.channel_id,
                    Draft.state.in_(["scheduled", "published"]),
                )
            )
            if d.scheduled_at
        ]
        draft.scheduled_at = next_slot(profile, occupied)
        draft.state = "scheduled"
        session.add(
            Approval(
                draft_id=draft.id,
                revision=draft.revision,
                content_hash=fingerprint(draft.current),
                config_id=draft.config_id,
                actor=actor,
            )
        )
        self.db.audit(session, actor, "approve", draft.id, revision=draft.revision)

    # Проверяем согласование текущей ревизии материала.
    def approve(self, draft_id, revision, content_hash, actor="editor"):
        self.refresh()
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            draft = session.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
            # Обрабатываем ветвь при условии: not draft.
            if not draft:
                raise AgentError("DRAFT_NOT_FOUND", 404)
            # Обрабатываем ветвь при условии: draft.revision != revision or fingerprint(draft.current) != content_hash.
            if draft.revision != revision or fingerprint(draft.current) != content_hash:
                raise AgentError("DRAFT_REVISION_CONFLICT", 409)
            # Обрабатываем ветвь при условии: draft.state not in {'review_pending', 'approved'}.
            if draft.state not in {"review_pending", "approved"}:
                raise AgentError("DRAFT_STATE_CONFLICT", 409)
            self._approve(session, draft, actor)

    # Создаём новую ревизию вместо изменения согласованного содержимого.
    def revise(self, draft_id, revision, body, actor="editor"):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            draft = session.get(Draft, draft_id)
            # Обрабатываем ветвь при условии: not draft or draft.revision != revision or draft.state == 'published'.
            if not draft or draft.revision != revision or draft.state == "published":
                raise AgentError("DRAFT_REVISION_CONFLICT", 409)
            intent = session.scalar(select(Intent).where(Intent.draft_id == draft_id))
            # Обрабатываем ветвь при условии: intent.
            if intent:
                raise AgentError("INTENT_ALREADY_CREATED", 409)
            materials = [
                serialize(m)
                for m in session.scalars(
                    select(Material).where(Material.story_id == draft.story_id)
                )
            ]
            candidate = {k: v for k, v in draft.current.items() if k not in {"checks", "video", "image"}}
            candidate["body"] = body
            candidate["checks"] = validate_draft(
                candidate,
                materials,
                self.settings.channel(draft.channel_id),
                allow_original=session.scalar(
                    select(VideoProject).where(VideoProject.draft_id == draft_id)
                )
                is not None,
            )
            # Обрабатываем ветвь при условии: 'video' in draft.current.
            if "video" in draft.current:
                candidate["video"] = draft.current["video"]
            # Обрабатываем ветвь при условии: 'image' in draft.current.
            if "image" in draft.current:
                candidate["image"] = draft.current["image"]
            draft.current, draft.revision, draft.state = (
                candidate,
                draft.revision + 1,
                "review_pending",
            )
            draft.scheduled_at = None
            draft.config_id = self.settings.hash
            session.execute(
                update(Approval).where(Approval.draft_id == draft_id).values(revoked=True)
            )
            session.add(
                DraftRevision(
                    draft_id=draft_id,
                    revision=draft.revision,
                    payload=candidate,
                    config_id=draft.config_id,
                )
            )
            self.db.audit(session, actor, "revise", draft_id, revision=draft.revision)

    # Отправляем согласованные материалы, срок публикации которых наступил.
    def publish_due(self):
        self.refresh()
        # Обрабатываем ветвь при условии: self.config_error.
        if self.config_error:
            return 0
        confirmed = 0
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            ids = session.scalars(
                select(Draft.id).where(Draft.state == "scheduled", Draft.scheduled_at <= now())
            ).all()
        # Последовательно обрабатываем элементы: ids.
        for draft_id in ids:
            # Record ownership of intent BEFORE the request. A crash leaves an inspectable
            # sending intent, never another automatic send after lease expiry.
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                draft = session.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
                profile = self.settings.channel(draft.channel_id)
                channel = session.scalar(
                    select(Channel).where(Channel.id == draft.channel_id).with_for_update()
                )
                # Обрабатываем ветвь при условии: channel.paused or not profile['enabled'] or profile['publication']['mode'] == 'dry_run'.
                if (
                    channel.paused
                    or not profile["enabled"]
                    or profile["publication"]["mode"] == "dry_run"
                ):
                    continue
                # Обрабатываем ветвь при условии: draft.config_id != self.settings.hash or not channel.chat_id.
                if draft.config_id != self.settings.hash or not channel.chat_id:
                    continue
                # Обрабатываем ветвь при условии: not channel.capabilities.get('publish').
                if not channel.capabilities.get("publish"):
                    continue
                # Обрабатываем ветвь при условии: self.publisher.mode == 'browser' and channel.capabilities.get('binding_hash') != fingerprint(self.settings.….
                if self.publisher.mode == "browser" and channel.capabilities.get("binding_hash") != fingerprint(self.settings.app["max"]):
                    continue
                approval = session.scalar(
                    select(Approval).where(
                        Approval.draft_id == draft_id,
                        Approval.revision == draft.revision,
                        Approval.revoked.is_(False),
                        Approval.config_id == self.settings.hash,
                        Approval.content_hash == fingerprint(draft.current),
                    )
                )
                # Обрабатываем ветвь при условии: not approval.
                if not approval:
                    continue
                previous = session.scalars(
                    select(Intent).where(
                        Intent.channel_id == channel.id, Intent.state == "confirmed"
                    )
                ).all()
                day = datetime.now(ZoneInfo(profile["publication"]["timezone"])).date()
                today_count = sum(
                    datetime.fromtimestamp(i.published_at, UTC)
                    .astimezone(ZoneInfo(profile["publication"]["timezone"]))
                    .date()
                    == day
                    for i in previous
                )
                gap = max(profile["publication"]["min_gap_minutes"] * 60, 1)
                # Обрабатываем ветвь при условии: today_count >= profile['publication']['max_posts_per_day'] or any((now() - i.published_at < gap for i in pr….
                if today_count >= profile["publication"]["max_posts_per_day"] or any(
                    now() - i.published_at < gap for i in previous
                ):
                    continue
                intent = session.scalar(select(Intent).where(Intent.draft_id == draft_id))
                # Обрабатываем ветвь при условии: intent and (intent.state not in {'retry_wait'} or intent.retry_after > now()).
                if intent and (intent.state not in {"retry_wait"} or intent.retry_after > now()):
                    continue
                # Обрабатываем ветвь при условии: not intent.
                if not intent:
                    intent = Intent(
                        draft_id=draft_id,
                        channel_id=channel.id,
                        body_hash=fingerprint(draft.current["body"]),
                    )
                    session.add(intent)
                intent.state, intent.send_started_at = "sending", now()
                session.flush()
                intent_id, chat_id, body = intent.id, channel.chat_id, draft.current["body"]
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session() as session:
                current = session.get(Draft, draft_id).current
                video, image = current.get("video"), current.get("image")
            # Обрабатываем ветвь при условии: video or image.
            if video or image:
                from .config import inside
                from .media import inspect_image, inspect_video

                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    # Обрабатываем ветвь при условии: video and image.
                    if video and image:
                        raise AgentError("MULTIPLE_ATTACHMENTS_FORBIDDEN")
                    media = video or image
                    inspected = (inspect_video(self.settings.root, media["path"]) if video
                                 else inspect_image(self.settings.root, media["path"]))
                    # Обрабатываем ветвь при условии: inspected['sha256'] != media['sha256'].
                    if inspected["sha256"] != media["sha256"]:
                        raise AgentError("VIDEO_HASH_CHANGED" if video else "IMAGE_HASH_CHANGED")
                    attachment = (self.publisher.upload_video if video else self.publisher.upload_image)(
                        inside(self.settings.root, media["path"])
                    )
                    # Обрабатываем ветвь при условии: self.publisher.mode == 'browser'.
                    if self.publisher.mode == "browser":
                        delivery = self.publisher.publish(chat_id, body, attachment, request_id=intent_id)
                    else:
                        # Альтернативная ветвь после проверки основного условия.
                        delivery = self.publisher.publish(chat_id, body, attachment)
                # Обрабатываем Exception и выбираем допустимый исход.
                except Exception:
                    from .adapters import Delivery

                    delivery = Delivery("unknown")
            else:
                # Обрабатываем ветвь при условии: self.publisher.mode == 'browser'.
                if self.publisher.mode == "browser":
                    delivery = self.publisher.publish(chat_id, body, request_id=intent_id)
                else:
                    # Альтернативная ветвь после проверки основного условия.
                    delivery = self.publisher.publish(chat_id, body)
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                intent = session.get(Intent, intent_id)
                intent.state = delivery.state
                intent.external_id, intent.permalink = delivery.external_id, delivery.permalink
                intent.retry_after = now() + delivery.retry_after
                # Обрабатываем ветвь при условии: delivery.state == 'confirmed'.
                if delivery.state == "confirmed":
                    intent.published_at = now()
                    session.get(Draft, draft_id).state = "published"
                    confirmed += 1
                # Обрабатываем ветвь при условии: delivery.state == 'rejected'.
                elif delivery.state == "rejected":
                    session.get(Channel, intent.channel_id).paused = True
                session.add(Attempt(intent_id=intent.id, result=delivery.state))
                self.db.audit(session, "publisher", delivery.state, intent.id)
        return confirmed

    # Сверяем сохранённую попытку без повторного нажатия отправки.
    def reconcile(self, intent_id):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            intent = session.get(Intent, intent_id)
            # Обрабатываем ветвь при условии: not intent.
            if not intent:
                raise AgentError("INTENT_NOT_FOUND", 404)
            # Обрабатываем ветвь при условии: intent.state not in {'unknown', 'sending'}.
            if intent.state not in {"unknown", "sending"}:
                return intent.state
            draft, channel = (
                session.get(Draft, intent.draft_id),
                session.get(Channel, intent.channel_id),
            )
            body, chat_id = draft.current["body"], channel.chat_id
        # Обрабатываем ветвь при условии: self.publisher.mode == 'browser'.
        if self.publisher.mode == "browser":
            delivery = self.publisher.reconcile(chat_id, intent_id)
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                intent = session.get(Intent, intent_id)
                intent.state = delivery.state
                # Обрабатываем ветвь при условии: delivery.state == 'confirmed'.
                if delivery.state == "confirmed":
                    intent.external_id, intent.permalink = delivery.external_id, delivery.permalink
                    intent.published_at = now()
                    session.get(Draft, intent.draft_id).state = "published"
                self.db.audit(session, "owner", "reconcile", intent_id, result=intent.state)
                return intent.state
        messages = self.publisher.messages(chat_id, count=100)
        matches = [m for m in messages if m.get("body", {}).get("text") == body]
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            intent = session.get(Intent, intent_id)
            # Обрабатываем ветвь при условии: len(matches) == 1 and matches[0].get('body', {}).get('mid').
            if len(matches) == 1 and matches[0].get("body", {}).get("mid"):
                intent.state = "confirmed"
                intent.external_id = str(matches[0]["body"]["mid"])
                intent.permalink = matches[0].get("url")
                intent.published_at = matches[0].get("timestamp", now() * 1000) / 1000
                session.get(Draft, intent.draft_id).state = "published"
            else:
                # Альтернативная ветвь после проверки основного условия.
                intent.state = "unknown"
            self.db.audit(session, "owner", "reconcile", intent_id, result=intent.state)
            return intent.state

    # Операция receive: очередь редакции, бюджет, согласование и журнал отправок.
    def receive(self, platform, payload):
        # Обрабатываем ветвь при условии: len(str(payload)) > self.settings.app['collection']['max_response_bytes'].
        if len(str(payload)) > self.settings.app["collection"]["max_response_bytes"]:
            raise AgentError("EVENT_TOO_LARGE", 413)
        key = str(payload.get("update_id")) if platform == "telegram" else fingerprint(payload)
        # Обрабатываем ветвь при условии: platform == 'telegram' and payload.get('update_id') is None.
        if platform == "telegram" and payload.get("update_id") is None:
            raise AgentError("INVALID_EVENT")
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                session.add(Inbox(platform=platform, event_key=key, payload=safe_payload(payload)))
            return True
        # Обрабатываем IntegrityError и выбираем допустимый исход.
        except IntegrityError:
            return False

    # Обрабатываем сохранённые входящие события.
    def process_inbox(self):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            ids = session.scalars(select(Inbox.id).where(Inbox.processed.is_(False))).all()
        # Последовательно обрабатываем элементы: ids.
        for event_id in ids:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session() as session:
                entry = session.get(Inbox, event_id)
                platform, payload = entry.platform, entry.payload
            # Последовательно обрабатываем элементы: self.settings.sources['sources'].
            for source in self.settings.sources["sources"]:
                # Обрабатываем ветвь при условии: source['enabled'] and source['type'] == platform + '_bot'.
                if source["enabled"] and source["type"] == platform + "_bot":
                    items = messenger_items(platform, payload, source)
                    # Обрабатываем ветвь при условии: items.
                    if items:
                        self.ingest(source["id"], items)
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                session.get(Inbox, event_id).processed = True
        return len(ids)

    # Выполняем один допустимый шаг обработки с учётом сохранённого состояния.
    def tick(self):
        self.refresh()
        self.hybrid.tick()
        self.schedule_sources()
        self.process_inbox()
        # Bound work per iteration; retries stop at max_read_attempts.
        # Последовательно обрабатываем элементы: range(100).
        for _ in range(100):
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                query = (
                    select(Job)
                    .where(
                        Job.state.in_(["queued", "retry_wait", "leased"]),
                        Job.due_at <= now(),
                        Job.lease_until <= now(),
                    )
                    .order_by(Job.due_at)
                    .with_for_update(skip_locked=True)
                )
                job = session.scalar(query)
                # Обрабатываем ветвь при условии: not job.
                if not job:
                    break
                job_id, kind, subject, key = job.id, job.kind, job.subject_id, job.key
                # Обрабатываем ветвь при условии: job.config_id != self.settings.hash.
                if job.config_id != self.settings.hash:
                    job.state = "cancelled"
                    continue
                claimed = session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.lease_until <= now(),
                        Job.state.in_(["queued", "retry_wait", "leased"]),
                    )
                    .values(
                        state="leased",
                        owner=self.worker_id,
                        lease_until=now() + self.settings.app["jobs"]["lease_seconds"],
                        attempts=Job.attempts + 1,
                    )
                )
                # Обрабатываем ветвь при условии: claimed.rowcount != 1.
                if claimed.rowcount != 1:
                    continue
            error = None
            heartbeat_stop = threading.Event()

            # Продлеваем аренду задания только текущему обработчику.
            def renew_lease(stop=heartbeat_stop, current_job_id=job_id):
                # Продолжаем цикл, пока выполняется условие: not stop.wait(self.settings.app['jobs']['heartbeat_seconds']).
                while not stop.wait(self.settings.app["jobs"]["heartbeat_seconds"]):
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with self.db.session.begin() as lease_session:
                        lease_session.execute(
                            update(Job)
                            .where(
                                Job.id == current_job_id,
                                Job.owner == self.worker_id,
                                Job.state == "leased",
                            )
                            .values(lease_until=now() + self.settings.app["jobs"]["lease_seconds"])
                        )

            heartbeat = threading.Thread(target=renew_lease, daemon=True)
            heartbeat.start()
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                # Обрабатываем ветвь при условии: kind == 'collect'.
                if kind == "collect":
                    self.collect(subject)
                else:
                    # Альтернативная ветвь после проверки основного условия.
                    channel_id = key.split(":")[1]
                    self.edit_story(channel_id, subject)
            # Обрабатываем AgentError и выбираем допустимый исход.
            except AgentError as exc:
                error = exc.code
            # Обрабатываем Exception и выбираем допустимый исход.
            except Exception:
                error = "JOB_INTERNAL_ERROR"
            finally:
                # Завершаем работу и освобождаем ресурсы независимо от результата.
                heartbeat_stop.set()
                heartbeat.join(timeout=1)
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                job = session.get(Job, job_id)
                # Обрабатываем ветвь при условии: job.owner != self.worker_id.
                if job.owner != self.worker_id:
                    continue
                permanent = error in {
                    "OUT_OF_SCOPE",
                    "EXCLUDED_TOPIC",
                    "STALE_SOURCE",
                    "RIGHTS_UNCLEAR",
                    "SOURCE_HTTP_401",
                    "SOURCE_HTTP_403",
                    "SOURCE_HTTP_429",
                    "SOURCE_CAPTCHA",
                    "SOURCE_PAUSED",
                    "SOURCE_CAPABILITY_UNSUPPORTED",
                }
                job.error, job.lease_until, job.owner = error, 0, None
                # Обрабатываем ветвь при условии: error.
                if error:
                    job.state = (
                        "dead_letter"
                        if permanent
                        or job.attempts >= self.settings.app["jobs"]["max_read_attempts"]
                        else "retry_wait"
                    )
                    job.due_at = now() + min(3600, 60 * 2**job.attempts)
                    # Обрабатываем ветвь при условии: kind == 'collect'.
                    if kind == "collect":
                        session.get(Cursor, subject).status = error
                else:
                    # Альтернативная ветвь после проверки основного условия.
                    job.state = "succeeded"
        return self.publish_due()

    # Операция report: очередь редакции, бюджет, согласование и журнал отправок.
    def report(self, channel_id):
        self.settings.channel(channel_id)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            metrics = [
                serialize(m)
                for m in session.scalars(select(Metric).where(Metric.channel_id == channel_id))
            ]
            intents = [
                serialize(i)
                for i in session.scalars(select(Intent).where(Intent.channel_id == channel_id))
            ]
            budgets = [
                serialize(b)
                for b in session.scalars(select(Budget).where(Budget.channel_id == channel_id))
            ]
            latest = {m["name"]: m["value"] for m in sorted(metrics, key=lambda m: m["created_at"])}
            return {
                "channel_id": channel_id,
                "publications": intents,
                "metrics": metrics,
                "subscribers": latest.get("subscribers", "unavailable"),
                "views": latest.get("views", "unavailable"),
                "budget": budgets,
                "content_cost_reserved_rub": sum(b["amount"] for b in budgets),
                "revenue": latest.get("revenue_rub", "unavailable"),
            }

    # Операция growth_plan: очередь редакции, бюджет, согласование и журнал отправок.
    def growth_plan(self, channel_id):
        profile = self.settings.channel(channel_id)
        plan = self.model_task(
            "growth_plan", channel_id, {"channel": profile, "report": self.report(channel_id)}
        )
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            campaign = Campaign(channel_id=channel_id, data=safe_payload(plan))
            session.add(campaign)
            session.flush()
            return serialize(campaign)

    # Освобождаем соединения и принадлежащие объекту ресурсы.
    def close(self):
        self.hybrid.close()
        self.http.close()
        self.editor.client.close()
        self.publisher.close()
        self.db.engine.dispose()
