"""Даты календаря и недельные часы публикаций в часовом поясе канала."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import AgentError


# Один план используется и календарём, и исполнителем, чтобы отображение совпадало с запуском.
def plan(config):
    return config.get("schedule", {"kind": "interval", "interval_hours": config["interval_hours"]})


# В интервале выбираем равномерные часы, в недельном плане — конкретные минуты и дни.
def hours_and_days(config):
    value = plan(config)
    if value["kind"] == "weekly":
        return sorted(value["times"]), sorted(value["weekdays"])
    return [f"{hour:02d}:00" for hour in range(0, 24, value["interval_hours"])], list(range(7))


# Пропускаем несуществующее местное время при переходе на летнее время.
def points(config, first, days):
    times, weekdays = hours_and_days(config)
    zone = ZoneInfo(config["timezone"])
    result = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        for value in times:
            hour, minute = map(int, value.split(":"))
            point = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if point.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != point.replace(tzinfo=None):
                continue
            result.append(point)
    return result


# Находим предыдущую и следующую точку; одно время при переводе часов выполняется один раз.
def weekly_window(config, current):
    candidates = points(config, current.date() - timedelta(days=7), 22)
    previous = [value for value in candidates if value.timestamp() <= current.timestamp()]
    following = [value for value in candidates if value.timestamp() > current.timestamp()]
    if not previous or not following:
        raise AgentError("NO_CALENDAR_SLOT")
    start, end = previous[-1], following[0]
    return start, end, start.strftime("%Y-%m-%d-%H%M%z")
