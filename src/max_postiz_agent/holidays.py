"""Локальный каталог праздников и однократный праздничный поиск каждого канала за день."""

import copy
import hashlib
import json
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from .channel_profiles import channel_path
from .config import AgentError, inside, parse_json, validate_schema

FILE = "config/holidays.json"
CATALOGS = ["russtate", "orthodox", "katolic", "evangelical", "islam", "jew", "buddhism",
            "hinduism", "bahai", "zoroastrianism", "slav", "pagan"]


# Проверка большого каталога выполняется один раз для версии файла, а не для каждой ячейки календаря.
@lru_cache(maxsize=32)
def cached_catalog(root, path, modified, size):
    value = parse_json(Path(path).read_text(encoding="utf-8-sig"))
    validate_schema(Path(root), "holidays.schema.json", value)
    ids = [item["id"] for item in value["holidays"]]
    if len(ids) != len(set(ids)):
        raise AgentError("HOLIDAY_ID_DUPLICATE")
    for item in value["holidays"]:
        try:
            date.fromisoformat(item["date"])
        except ValueError as exc:
            raise AgentError("HOLIDAY_DATE_INVALID") from exc
    return value


# Каталог хранит названия и даты — описания статей и изображения источника не копируются.
def catalog(root):
    path = inside(root, FILE)
    if not path.exists():
        path = inside(root, "config/holidays.example.json")
    if not path.exists():
        return {"version": 1, "enabled": False, "calendar_year": 2026, "holidays": []}
    stat = path.stat()
    return copy.deepcopy(cached_catalog(str(root), str(path), stat.st_mtime_ns, stat.st_size))


# Православная пасхалия переводится из юлианского календаря в современную гражданскую дату.
def easter(year):
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = (d + e + 114) // 31, (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=year // 100 - year // 400 - 2)


# Только подтверждённые правила повторения переносятся на следующий год.
def holiday_date(item, year):
    rule = item.get("rule")
    original = date.fromisoformat(item["date"])
    if original.year == year:
        return original
    if not rule:
        return None
    if rule["kind"] == "fixed":
        try:
            return date(year, rule["month"], rule["day"])
        except ValueError:
            return None
    if rule["kind"] == "easter_relative":
        return easter(year) + timedelta(days=rule["offset"])
    first = date(year, rule["month"], 1)
    result = first + timedelta(days=(rule["weekday"] - first.weekday()) % 7)
    if rule["nth"] == -1:
        while (result + timedelta(days=7)).month == first.month:
            result += timedelta(days=7)
    else:
        result += timedelta(days=7 * (rule["nth"] - 1))
    return result if result.month == first.month else None


# Календарь показывает праздники даже когда тематический поиск выключен настройкой.
def for_day(root, day):
    return [{**item, "date": day.isoformat()} for item in catalog(root)["holidays"]
            if holiday_date(item, day.year) == day]


# Первая попытка сохраняется до сбора; последующие интервалы и перезапуск используют обычные запросы.
def campaign(root, channel_id, current, *, reserve=False, slot=None):
    value = catalog(root)
    if not value["enabled"]:
        return None
    day = current.date()
    marker = inside(root, "data/image-ranking/" + channel_path(channel_id, f"holiday-days/{day.isoformat()}.json"))
    runs = inside(root, "data/image-ranking/" + channel_path(channel_id, "auto-runs"))
    # Старые версии тоже могли уже выполнить первый запуск дня; их журнал учитываем при обновлении.
    previous = [path for path in runs.glob(day.isoformat() + "-*.json") if path.stem != slot]
    first = not marker.exists() and not previous
    holidays = for_day(root, day) if first else []
    result = {"first_run_of_day": first, "date": day.isoformat(),
              "holiday_names": [item["name"] for item in holidays],
              "search_tags": [item["name"] for item in holidays]}
    if reserve and first:
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x", encoding="utf-8") as output:
                json.dump(result, output, ensure_ascii=False, indent=2)
        except FileExistsError:
            return {"first_run_of_day": False, "date": day.isoformat(), "holiday_names": [], "search_tags": []}
    return result


# Имена праздников становятся поисковыми тегами; англоязычные уточнения помогают локальной CLIP.
def search_config(root, config, campaign_info):
    if not campaign_info or not campaign_info["holiday_names"]:
        return config
    cfg = copy.deepcopy(config)
    items = for_day(root, date.fromisoformat(campaign_info["date"]))
    names = campaign_info["holiday_names"]
    extra = list(dict.fromkeys(tag for item in items for tag in item.get("tags", [])))
    queries = [name[:100] for name in names]
    if extra:
        queries.append(" ".join(extra)[:100])
    # Ограничиваем общий объём ответов прежним бюджетом скачивания, а подборку — одним постом.
    for index, source in enumerate(cfg["sources"]):
        if source["type"] == "pixabay_api":
            source["queries"] = queries
        else:
            source["query"] = queries[index % len(queries)]
    cfg["top_n"] = 1
    subject = ", ".join(extra) if extra else ", ".join(names)
    cfg["local_prompts"] = ["high quality illustration celebrating " + subject,
                            "unrelated image, screenshot, text advertisement or poor quality"]
    cfg["holiday_names"] = names
    return cfg


# Импорт извлекает только факты каталога России и прекращается при блокировке источника.
def import_catalog(root):
    from bs4 import BeautifulSoup
    from .network import SafeHTTP
    from .channel_profiles import atomic_json
    rows, seen = [], {}
    client = SafeHTTP(max_bytes=5_242_880, redirects=2)
    try:
        for category in CATALOGS:
            url = "https://www.calend.ru/holidays/" + category + "/"
            status, content, _, _ = client.get(url)
            if status != 200:
                raise AgentError("HOLIDAY_SOURCE_UNAVAILABLE")
            soup = BeautifulSoup(content, "html.parser")
            count = 0
            for item in soup.select(".datesList li"):
                title, day = item.select_one(".caption > .title > a[href]"), item.select_one(".dataNum a[href]")
                if not title or not day:
                    continue
                numbers = re.search(r"/day/(\d{4})-(\d{1,2})-(\d{1,2})/", day["href"])
                if not numbers:
                    continue
                stamp = date(*map(int, numbers.groups())).isoformat()
                name = title.get_text(" ", strip=True)
                source = title["href"].replace("http://", "https://")
                if source.startswith("/"):
                    source = "https://www.calend.ru" + source
                entity = re.search(r"/holidays/0/0/(\d+)/", source)
                key = (entity[1] if entity else name.casefold(), stamp)
                count += 1
                if key in seen:
                    continue
                row = {"id": hashlib.sha256((name + stamp).encode()).hexdigest()[:16],
                       "name": name, "date": stamp, "source_url": source, "tags": [], "category": category}
                # Совпадающие имена и даты тоже представлены одной записью.
                if any(existing["name"].casefold() == name.casefold() and existing["date"] == stamp for existing in rows):
                    continue
                rows.append(row)
                seen[key] = row
            if count < (200 if category == "russtate" else 1):
                raise AgentError("HOLIDAY_SOURCE_LIST_INCOMPLETE")
    finally:
        client.close()
    old = catalog(root)
    year = max(date.fromisoformat(row["date"]).year for row in rows)
    # Сохраняем пользовательские уточнения и дополнительные религиозные даты.
    known = {item["source_url"]: item for item in old["holidays"]}
    for row in rows:
        for field in ("rule", "tags", "category"):
            if field in known.get(row["source_url"], {}):
                row[field] = known[row["source_url"]][field]
    urls = {row["source_url"] for row in rows}
    rows.extend(item for item in old["holidays"] if item["source_url"] not in urls)
    value = {"version": 1, "enabled": old["enabled"], "calendar_year": year, "holidays": rows}
    validate_schema(root, "holidays.schema.json", value)
    atomic_json(root, FILE, value)
    return {"state": "updated", "holiday_count": len(rows), "calendar_year": year, "source_url": "https://www.calend.ru/holidays/russtate/"}
