"""Тематика, отбор изображений и границы расписания для каждого канала."""

import copy
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import AgentError, Settings, fingerprint, inside, parse_json, validate_schema

PROFILE_FILE = "config/image_channels.json"
DEFAULT_PROMPTS = [
    "high quality imaginative AI generated digital artwork, vivid composition",
    "unrelated ordinary snapshot, text screenshot or low quality image",
]


# Разрешаем только безопасные идентификаторы профилей.
def validate_channel_id(channel_id):
    # Идентификатор входит в имена каталогов; запрещаем обход пути и случайные адресаты.
    if not isinstance(channel_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel_id):
        raise AgentError("IMAGE_CHANNEL_ID_INVALID")
    return channel_id


# Загружаем профили изображений и проверяем принадлежность каналам.
def profiles(root):
    # Отсутствующий файл означает совместимость с установкой, где был один канал.
    path = inside(root, PROFILE_FILE)
    data = parse_json(path.read_text(encoding="utf-8-sig")) if path.exists() else {
        "version": 1, "channels": {},
    }
    validate_schema(root, "image_channels.schema.json", data)
    topic_ids = {row["id"] for row in Settings.load(root).topic["channels"]}
    # Обрабатываем ветвь при условии: not set(data['channels']) <= topic_ids.
    if not set(data["channels"]) <= topic_ids:
        raise AgentError("UNKNOWN_CHANNEL")
    return data


# Накладываем настройки одного канала на общий каталог источников.
def image_config(root, catalog, channel_id=None):
    # Каталог источников общий, поисковые запросы и фильтры принадлежат одному каналу.
    channel_id = validate_channel_id(channel_id or catalog["target_channel"])
    settings = Settings.load(root)
    topic = settings.channel(channel_id)
    data = profiles(root)
    explicit = channel_id in data["channels"]
    # Обрабатываем ветвь при условии: not explicit and channel_id != catalog['target_channel'].
    if not explicit and channel_id != catalog["target_channel"]:
        raise AgentError("IMAGE_CHANNEL_PROFILE_REQUIRED")
    profile = data["channels"].get(channel_id, {})
    cfg = copy.deepcopy(catalog)
    cfg["target_channel"] = channel_id
    # Последовательно обрабатываем элементы: profile.get('filters', {}).items().
    for key, value in profile.get("filters", {}).items():
        cfg[key] = value
    # Последовательно обрабатываем элементы: cfg['sources'].
    for source in cfg["sources"]:
        # Обрабатываем ветвь при условии: 'source_ids' in profile.
        if "source_ids" in profile:
            source["enabled"] = source["enabled"] and source["id"] in profile["source_ids"]
        # Обрабатываем ветвь при условии: 'queries' in profile.
        if "queries" in profile:
            # Обрабатываем ветвь при условии: source['type'] == 'pixabay_api'.
            if source["type"] == "pixabay_api":
                source["queries"] = profile["queries"]
                source["image_type"] = profile.get("image_type", "illustration")
            else:
                # Альтернативная ветвь после проверки основного условия.
                source["query"] = profile["queries"][0]
    # Обрабатываем ветвь при условии: set(profile.get('source_ids', [])) - {row['id'] for row in cfg['sources']}.
    if set(profile.get("source_ids", [])) - {row["id"] for row in cfg["sources"]}:
        raise AgentError("UNKNOWN_IMAGE_SOURCE")
    cfg["local_prompts"] = profile.get("local_prompts", DEFAULT_PROMPTS)
    cfg["caption_title"] = profile.get("caption_title", "Иллюстрация из открытой коллекции")
    cfg["timezone"] = topic["publication"]["timezone"]
    cfg["interval_hours"] = profile.get("interval_hours", 4)
    if "schedule" in profile:
        cfg["schedule"] = profile["schedule"]
    cfg["profile_explicit"] = explicit
    cfg["profile_hash"] = fingerprint({"topic": topic, "images": cfg})
    return cfg


# Разделяем файлы каналов, сохраняя совместимость старых путей.
def channel_path(channel_id, name):
    # Сохраняем старые пути test: журнал доставок продолжает защищать от повторов.
    validate_channel_id(channel_id)
    return name if channel_id == "test" else f"channels/{channel_id}/{name}"


# Вычисляем границы интервала в часовом поясе выбранного канала.
def window(root, channel_id="test", at=None):
    from .image_pipeline import source_config

    # Интервалы задаются в часовом поясе канала, следующий запуск берём по шкале UTC.
    cfg = source_config(root, channel_id)
    zone = ZoneInfo(cfg["timezone"])
    current = (at or datetime.now(zone)).astimezone(zone)
    from .calendar_schedule import plan, weekly_window
    if plan(cfg)["kind"] == "weekly":
        start, following, slot = weekly_window(cfg, current)
        return current, start, following, slot, cfg
    interval = plan(cfg)["interval_hours"]
    start = current.replace(hour=current.hour // interval * interval,
                            minute=0, second=0, microsecond=0)
    following = datetime.fromtimestamp(start.timestamp() + interval * 3600, zone)
    slot = start.strftime("%Y-%m-%d-%H")
    # Обрабатываем ветвь при условии: channel_id != 'test'.
    if channel_id != "test":
        slot += start.strftime("%z")
    return current, start, following, slot, cfg


# Заменяем JSON-файл только после полной записи нового содержимого.
def atomic_json(root, relative, value):
    import json

    # Сначала записываем полный файл, затем заменяем его без промежуточного JSON.
    path = inside(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
