"""Применение профилей каналов с сохранением истории и остановкой отправки."""

import copy
import json
import re

from sqlalchemy import select

from .channel_profiles import PROFILE_FILE, atomic_json, profiles, validate_channel_id
from .config import AgentError, Settings, inside, validate_schema
from .db import Channel, Intent


# Объединяем настройки каналов, подключения и состояние расписаний.
def list_channels(service):
    from .image_schedule import status
    from .image_pipeline import source_config

    # Показываем настройки вместе с фактическим подключением и отдельным расписанием.
    images = profiles(service.settings.root)["channels"]
    with service.db.session() as session:
        rows = []
        # Последовательно обрабатываем элементы: service.settings.topic['channels'].
        for profile in service.settings.topic["channels"]:
            row = session.get(Channel, profile["id"])
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                schedule = status(service.settings.root, channel_id=profile["id"])
                # Возвращаем редактируемый профиль и для прежней одноканальной установки.
                effective = source_config(service.settings.root, profile["id"])
                fallback = {"queries": next((row["queries"] for row in effective["sources"]
                                             if row["type"] == "pixabay_api"),
                                            [effective["sources"][0].get("query", "digital art")]),
                            "local_prompts": effective["local_prompts"],
                            "caption_title": effective["caption_title"],
                            "interval_hours": effective["interval_hours"], "image_type": "illustration"}
            # Обрабатываем AgentError и выбираем допустимый исход.
            except AgentError as exc:
                schedule = {"state": "not_configured", "code": exc.code}
                fallback = None
            rows.append({"channel": profile, "images": images.get(profile["id"], fallback),
                         "chat_id": row.chat_id if row else None,
                         "paused": row.paused if row else True, "schedule": schedule})
        return rows


# Проверяем полный профиль, сохраняем его и останавливаем отправку канала.
def configure_channel(service, document):
    from .image_schedule import lock, read, write
    from .image_pipeline import source_config

    # Принимаем полный профиль: смена темы включает аудиторию, рубрики и ограничения.
    if not isinstance(document, dict) or set(document) - {"channel", "images", "chat_id", "chat_title"}:
        raise AgentError("CHANNEL_DOCUMENT_INVALID")
    profile = document.get("channel", {})
    channel_id = validate_channel_id(profile.get("id"))
    root = service.settings.root
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with lock(root, "run"), lock(root, "control"):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session() as session:
            # Обрабатываем ветвь при условии: session.scalar(select(Intent).where(Intent.channel_id == channel_id, Intent.state.in_(['pending', 'sending'….
            if session.scalar(select(Intent).where(
                Intent.channel_id == channel_id,
                Intent.state.in_(["pending", "sending", "unknown", "retry_wait"]),
            )):
                raise AgentError("CHANNEL_UNRESOLVED_PUBLICATION", 409)
        topic = copy.deepcopy(service.settings.topic)
        topic["channels"] = [row for row in topic["channels"] if row["id"] != channel_id] + [profile]
        topic["revision"] += 1
        validate_schema(root, "topic.schema.json", topic)
        image_profiles = profiles(root)
        # Обрабатываем ветвь при условии: 'images' not in document.
        if "images" not in document:
            raise AgentError("CHANNEL_IMAGE_PROFILE_REQUIRED")
        image_profiles["channels"][channel_id] = document["images"]
        validate_schema(root, "image_channels.schema.json", image_profiles)
        app = copy.deepcopy(service.settings.app)
        # Обрабатываем ветвь при условии: document.get('chat_id') is not None.
        if document.get("chat_id") is not None:
            chat_id, title = str(document["chat_id"]), document.get("chat_title")
            # Обрабатываем ветвь при условии: not re.fullmatch('-?\\d+', chat_id) or not isinstance(title, str) or (not title.strip()).
            if not re.fullmatch(r"-?\d+", chat_id) or not isinstance(title, str) or not title.strip():
                raise AgentError("BROWSER_CHANNEL_NOT_ALLOWED")
            # Одному адресу MAX соответствует один профиль, чтобы не смешивать тематики.
            with service.db.session() as session:
                # Обрабатываем ветвь при условии: session.scalar(select(Channel).where(Channel.chat_id == chat_id, Channel.id != channel_id)).
                if session.scalar(select(Channel).where(Channel.chat_id == chat_id,
                                                         Channel.id != channel_id)):
                    raise AgentError("CHANNEL_ADDRESS_ALREADY_BOUND")
            app["max"].setdefault("browser", {}).setdefault("channels", {})[chat_id] = title
        topic_path = inside(root, app["topic_path"])
        text = topic_path.read_text(encoding="utf-8-sig")
        pattern = r"(<!-- TOPIC_CONFIG_BEGIN -->\s*```json\s*)(.*?)(\s*```\s*<!-- TOPIC_CONFIG_END -->)"
        updated = re.sub(pattern, lambda m: m[1] + json.dumps(topic, ensure_ascii=False, indent=2) + m[3], text, flags=re.S)
        # Откатываем файлы при ошибке проверки; базу меняем только после успешной проверки.
        paths = [topic_path, inside(root, PROFILE_FILE), inside(root, "config/app.json")]
        old = {path: path.read_bytes() if path.exists() else None for path in paths}
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            temporary = topic_path.with_suffix(".new")
            temporary.write_text(updated, encoding="utf-8")
            temporary.replace(topic_path)
            atomic_json(root, PROFILE_FILE, image_profiles)
            atomic_json(root, "config/app.json", app)
            settings = Settings.load(root)
            source_config(root, channel_id)
            service.activate(settings)
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            # Последовательно обрабатываем элементы: old.items().
            for path, value in old.items():
                # Обрабатываем ветвь при условии: value is None.
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    # Альтернативная ветвь после проверки основного условия.
                    path.write_bytes(value)
            raise
        service.pause(channel_id, True, actor="owner")
        control = read(root, "control", {}, channel_id)
        write(root, "control", {**control, "enabled": False, "reason": "configuration_changed"}, channel_id)
        return {"state": "configured", "channel_id": channel_id, "paused": True,
                "schedule_enabled": False, "config_hash": settings.hash}
