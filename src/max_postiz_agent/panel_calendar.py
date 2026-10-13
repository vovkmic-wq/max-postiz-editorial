"""Недельный календарь реальных постов, черновиков и запланированных автопубликаций."""

import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .calendar_schedule import hours_and_days, points
from .config import AgentError
from .db import Draft, Intent

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


# Подборки будущего времени показаны отдельно от уже созданных и опубликованных материалов.
def calendar(service, channel_id, start=None, *, at=None):
    from .image_pipeline import source_config
    from .image_schedule import read, status
    channel = service.settings.channel(channel_id)
    zone = ZoneInfo(channel["publication"]["timezone"])
    current = (at or datetime.now(zone)).astimezone(zone)
    try:
        selected = datetime.strptime(start, "%Y-%m-%d").date() if start else current.date()
    except (ValueError, TypeError) as exc:
        raise AgentError("CALENDAR_DATE_INVALID") from exc
    first = selected - timedelta(days=selected.weekday())
    days = [{"date": (first + timedelta(days=n)).isoformat(), "weekday": WEEKDAYS[n],
             "is_today": first + timedelta(days=n) == current.date(), "events": []} for n in range(7)]
    from .holidays import for_day, campaign, catalog
    holiday_catalog = catalog(service.settings.root)
    for day in days:
        from datetime import date
        day["holidays"] = for_day(service.settings.root, date.fromisoformat(day["date"]))
    by_date = {day["date"]: day for day in days}
    events = []
    with service.db.session() as session:
        drafts = list(session.scalars(select(Draft).where(Draft.channel_id == channel_id)))
        for draft in drafts:
            intent = session.scalar(select(Intent).where(Intent.draft_id == draft.id))
            stamp = intent.published_at if intent and intent.state == "confirmed" else draft.scheduled_at
            if not stamp or (draft.state != "scheduled" and not (intent and intent.state == "confirmed")):
                continue
            local = datetime.fromtimestamp(stamp, zone)
            if local.date().isoformat() not in by_date:
                continue
            event = {"id": draft.id, "kind": "publication", "state": "published" if intent and intent.state == "confirmed" else "scheduled",
                     "at": local.isoformat(), "time": local.strftime("%H:%M"),
                     "title": draft.current.get("title", "Публикация"), "body": draft.current.get("body", ""),
                     "revision": draft.revision, "intent_id": intent.id if intent else None,
                     "image": bool(draft.current.get("image")), "video": bool(draft.current.get("video"))}
            events.append(event)
    try:
        cfg = source_config(service.settings.root, channel_id)
        schedule = status(service.settings.root, channel_id=channel_id, at=current)
        control = read(service.settings.root, "control", {}, channel_id)
        holiday_days = set()
        for point in points(cfg, first, 7):
            # Фактический пост заменяет ожидаемую карточку того же интервала.
            if any(abs(datetime.fromisoformat(event["at"]).timestamp() - point.timestamp()) <= 300 for event in events):
                continue
            if point.timestamp() < current.timestamp():
                continue
            not_before = datetime.fromisoformat(control["not_before"]) if control.get("not_before") else current
            enabled = schedule["state"] == "enabled" and point.timestamp() >= not_before.timestamp()
            holiday = campaign(service.settings.root, channel_id, point)
            tags = holiday["holiday_names"] if holiday and point.date() not in holiday_days else []
            holiday_days.add(point.date())
            events.append({"holiday_tags": tags, "id": "auto-" + point.isoformat(), "kind": "automatic", "state": "planned" if enabled else "paused",
                           "at": point.isoformat(), "time": point.strftime("%H:%M"),
                           "title": "Автоматическая публикация изображения", "body": "Изображение будет выбрано локальной моделью перед отправкой.",
                           "image": True})
        times, weekdays = hours_and_days(cfg)
        schedule["times"], schedule["weekdays"] = times, weekdays
        from .calendar_schedule import plan
        schedule["plan"] = plan(cfg)
    except AgentError as exc:
        schedule = {"state": "not_configured", "code": exc.code,
                    "times": channel["publication"]["slots"], "weekdays": channel["publication"].get("weekdays", list(range(7)))}
    for event in events:
        by_date[datetime.fromisoformat(event["at"]).date().isoformat()]["events"].append(event)
    for day in days:
        day["events"].sort(key=lambda event: event["at"])
    return {"channel_id": channel_id, "timezone": str(zone), "today": current.date().isoformat(),
            "week_start": first.isoformat(), "week_end": (first + timedelta(days=6)).isoformat(),
            "days": days, "holiday_search_enabled": holiday_catalog["enabled"], "holiday_count": len(holiday_catalog["holidays"]), "holiday_catalog_year": holiday_catalog["calendar_year"], "schedule": schedule, "publication_policy": channel["publication"]}


# Форма меняет одновременно дни редакции и недельное расписание изображений.
def save_schedule(service, channel_id, values):
    from .channel_management import configure_channel, list_channels
    from .image_schedule import configure
    if not isinstance(values, dict) or set(values) - {"kind", "interval_hours", "weekdays", "times", "timezone", "enabled", "min_gap_minutes", "max_posts_per_day"}:
        raise AgentError("SCHEDULE_DOCUMENT_INVALID")
    if not isinstance(values.get("enabled"), bool):
        raise AgentError("SCHEDULE_DOCUMENT_INVALID")
    row = next(item for item in list_channels(service) if item["channel"]["id"] == channel_id)
    if not row["images"]:
        raise AgentError("IMAGE_CHANNEL_PROFILE_REQUIRED")
    topic, images = copy.deepcopy(row["channel"]), copy.deepcopy(row["images"])
    images["schedule"] = {"kind": "weekly", "times": values.get("times"), "weekdays": values.get("weekdays")}
    if values.get("kind") == "interval":
        images["schedule"] = {"kind": "interval", "interval_hours": values.get("interval_hours")}
    topic["publication"].update(timezone=values.get("timezone"), slots=values.get("times"), weekdays=values.get("weekdays"),
                                min_gap_minutes=values.get("min_gap_minutes"), max_posts_per_day=values.get("max_posts_per_day"))
    # Проверяем ограничения до применения профиля; ошибка оставляет прежнее расписание.
    from .panel_settings import snapshot, validate_configuration
    candidate = snapshot(service)["configuration"]
    candidate["topic"]["channels"] = [item if item["id"] != channel_id else topic for item in candidate["topic"]["channels"]]
    candidate["image_channels"]["channels"][channel_id] = images
    validate_configuration(service.settings.root, candidate)
    result = configure_channel(service, {"channel": topic, "images": images})
    result["schedule"] = configure(service.settings.root, values["enabled"], channel_id=channel_id)
    return result
