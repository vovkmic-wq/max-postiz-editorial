# Редакционные ограничения, планирование публикаций и проверка текста.
"""Deterministic editorial gates around model output."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .collectors import detect_language
from .config import AgentError


# Извлекаем числа для проверки подтверждённых утверждений.
def numbers(text):
    return set(re.findall(r"\d+(?:[.,]\d+)*", text))


# Проверяем условия допустимого перевода материала.
def translation_gate(original, translated):
    # Обрабатываем ветвь при условии: numbers(original) != numbers(translated).
    if numbers(original) != numbers(translated):
        raise AgentError("TRANSLATION_NUMBER_CHANGED")
    # Обрабатываем ветвь при условии: detect_language(translated) != 'ru'.
    if detect_language(translated) != "ru":
        raise AgentError("TRANSLATION_NOT_RUSSIAN")
    # Negation and semantic alignment require editorial review in addition to this numerical gate.


# Проверяем язык, факты, ограничения канала и редакционные требования.
def validate_draft(value, materials, channel, model_verified=False, allow_original=False):
    required = {
        "title",
        "body",
        "language",
        "source_ids",
        "decision",
        "reason_codes",
        "claims",
        "confidence",
        "payload",
    }
    # Обрабатываем ветвь при условии: set(value) != required.
    if set(value) != required:
        raise AgentError("EDITOR_RESULT_FIELDS_INVALID")
    # Обрабатываем ветвь при условии: not isinstance(value['body'], str) or not value['body'].strip().
    if not isinstance(value["body"], str) or not value["body"].strip():
        raise AgentError("EDITOR_EMPTY_BODY")
    # Обрабатываем ветвь при условии: not isinstance(value['title'], str) or not isinstance(value['confidence'], (float, int)).
    if not isinstance(value["title"], str) or not isinstance(value["confidence"], (float, int)):
        raise AgentError("EDITOR_RESULT_TYPES_INVALID")
    # Обрабатываем ветвь при условии: not 0 <= value['confidence'] <= 1 or value['decision'] not in {'accept', 'review', 'reject'}.
    if not 0 <= value["confidence"] <= 1 or value["decision"] not in {"accept", "review", "reject"}:
        raise AgentError("EDITOR_RESULT_TYPES_INVALID")
    # Обрабатываем ветвь при условии: not isinstance(value['claims'], list) or not isinstance(value['reason_codes'], list).
    if not isinstance(value["claims"], list) or not isinstance(value["reason_codes"], list):
        raise AgentError("EDITOR_RESULT_TYPES_INVALID")
    ids = {m["id"]: m for m in materials}
    # Обрабатываем ветвь при условии: not isinstance(value['source_ids'], list) or (not value['source_ids'] and (not allow_original)) or (not set….
    if (
        not isinstance(value["source_ids"], list)
        or (not value["source_ids"] and not allow_original)
        or not set(value["source_ids"]) <= set(ids)
    ):
        raise AgentError("EDITOR_UNKNOWN_SOURCE")
    body = value["body"]
    reasons = []
    # Обрабатываем ветвь при условии: value['language'] != 'ru' or detect_language(body) != 'ru'.
    if value["language"] != "ru" or detect_language(body) != "ru":
        reasons.append("OUTPUT_NOT_RUSSIAN")
    # Обрабатываем ветвь при условии: len(body) > min(4000, channel['publication']['max_chars']).
    if len(body) > channel["publication"]["max_chars"]:
        reasons.append("LENGTH_EXCEEDED")
    # Обрабатываем ветвь при условии: any((term.casefold() in body.casefold() for term in channel['exclude_topics'])).
    if any(term.casefold() in body.casefold() for term in channel["exclude_topics"]):
        reasons.append("EXCLUDED_TOPIC")
    texts = " ".join(m["text"] + " " + m["title"] for m in materials)
    # Обрабатываем ветвь при условии: not allow_original and (not numbers(body) <= numbers(texts + ' '.join((m['url'] for m in materials)))).
    if not allow_original and not numbers(body) <= numbers(
        texts + " ".join(m["url"] for m in materials)
    ):
        reasons.append("UNSUPPORTED_NUMBER")
    # Последовательно обрабатываем элементы: value['source_ids'].
    for mid in value["source_ids"]:
        # Обрабатываем ветвь при условии: not ids[mid]['url'] or ids[mid]['url'] not in body.
        if not ids[mid]["url"] or ids[mid]["url"] not in body:
            reasons.append("MISSING_SOURCE_LINK")
    # Последовательно обрабатываем элементы: value['claims'].
    for claim in value["claims"]:
        # Обрабатываем ветвь при условии: not isinstance(claim, dict) or set(claim) != {'text', 'material_id', 'quote'}.
        if not isinstance(claim, dict) or set(claim) != {"text", "material_id", "quote"}:
            raise AgentError("CLAIM_SCHEMA_INVALID")
        mid, quote = claim["material_id"], claim["quote"]
        # Обрабатываем ветвь при условии: mid not in ids or not quote or quote not in ids[mid]['text'].
        if mid not in ids or not quote or quote not in ids[mid]["text"]:
            reasons.append("INSUFFICIENT_EVIDENCE")
        # Обрабатываем ветвь при условии: claim['text'] not in body.
        if claim["text"] not in body:
            reasons.append("CLAIM_NOT_IN_BODY")
    # Обрабатываем ветвь при условии: not value['claims'] or not model_verified.
    if not value["claims"] or not model_verified:
        reasons.append("EDITOR_REVIEW_REQUIRED")
    return sorted(set(reasons))


# Выбираем ближайшее разрешённое время публикации.
def next_slot(channel, occupied, earliest=None):
    policy = channel["publication"]
    zone = ZoneInfo(policy["timezone"])
    start = datetime.fromtimestamp(earliest, UTC) if earliest else datetime.now(UTC)
    local = start.astimezone(zone)
    # Последовательно обрабатываем элементы: range(31).
    for offset in range(31):
        day = local.date() + timedelta(days=offset)
        # Учитываем выбранные дни недели при назначении одобренного черновика.
        if day.weekday() not in policy.get("weekdays", range(7)):
            continue
        count = sum(datetime.fromtimestamp(t, UTC).astimezone(zone).date() == day for t in occupied)
        # Обрабатываем ветвь при условии: count >= policy['max_posts_per_day'].
        if count >= policy["max_posts_per_day"]:
            continue
        # Последовательно обрабатываем элементы: sorted(policy['slots']).
        for slot in sorted(policy["slots"]):
            hour, minute = map(int, slot.split(":"))
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            # Reject nonexistent local times (DST gaps). Choose fold=0 for ambiguous times.
            # Обрабатываем ветвь при условии: candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None).
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != candidate.replace(
                tzinfo=None
            ):
                continue
            stamp = candidate.timestamp()
            # Обрабатываем ветвь при условии: stamp < start.timestamp().
            if stamp < start.timestamp():
                continue
            # Обрабатываем ветвь при условии: all((abs(stamp - t) >= policy['min_gap_minutes'] * 60 for t in occupied)).
            if all(abs(stamp - t) >= policy["min_gap_minutes"] * 60 for t in occupied):
                return stamp
    raise AgentError("NO_CALENDAR_SLOT")
