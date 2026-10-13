# Сбор материалов, очистка текста и определение языка.
"""RSS, HTML and authorized messenger collectors."""

import calendar
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import feedparser
from bs4 import BeautifulSoup

from .config import AgentError
from .network import canonical_url


# Структура Item объединяет связанные данные и операции модуля.
@dataclass
class Item:
    external_id: str
    title: str
    text: str
    url: str
    published_at: float | None = None
    language: str = "auto"
    media: list = field(default_factory=list)


# Извлекаем текст без исполняемой разметки.
def clean_html(text):
    soup = BeautifulSoup(text, "html.parser")
    # Последовательно обрабатываем элементы: soup.select('script,style,nav,footer,header,aside').
    for node in soup.select("script,style,nav,footer,header,aside"):
        node.decompose()
    return " ".join(soup.get_text(" ").split())


# Оцениваем язык текста для редакционных ограничений.
def detect_language(text):
    text = re.sub(r"https?://\S+", "", text)
    ru = len(re.findall("[а-яА-ЯёЁ]", text))
    latin = len(re.findall("[a-zA-Z]", text))
    return "ru" if ru > latin else "en"


# Собираем материалы разрешённого веб-источника.
def collect_web(source, cursor, http, root):
    if source["type"] == "json_api":
        from .json_source import collect_json
        return collect_json(source, cursor, http, root)
    headers = {}
    # Обрабатываем ветвь при условии: cursor.get('etag').
    if cursor.get("etag"):
        headers["If-None-Match"] = cursor["etag"]
    # Обрабатываем ветвь при условии: cursor.get('last_modified').
    if cursor.get("last_modified"):
        headers["If-Modified-Since"] = cursor["last_modified"]
    status, content, returned_headers, url = http.get(source["endpoint"], headers)
    # Обрабатываем ветвь при условии: status == 304.
    if status == 304:
        return [], cursor
    items = []
    # Обрабатываем ветвь при условии: source['type'] == 'rss'.
    if source["type"] == "rss":
        feed = feedparser.parse(content)
        # Обрабатываем ветвь при условии: not feed.entries and (feed.bozo or not feed.feed).
        if not feed.entries and (feed.bozo or not feed.feed):
            raise AgentError("INVALID_FEED")
        # Последовательно обрабатываем элементы: feed.entries[:source['max_items_per_run']].
        for entry in feed.entries[: source["max_items_per_run"]]:
            link = entry.get("link", "")
            # Обрабатываем ветвь при условии: not link.
            if not link:
                continue
            text = clean_html(entry.get("summary", entry.get("description", "")))
            # Обрабатываем ветвь при условии: source['full_text'] and entry.get('content').
            if source["full_text"] and entry.get("content"):
                text = clean_html(entry.content[0].get("value", text))
            time = entry.get("published_parsed")
            # Обрабатываем ветвь при условии: time is None.
            if time is None:
                time = entry.get("updated_parsed")
            published = calendar.timegm(time) if time else None
            items.append(
                Item(
                    str(entry.get("id", link)),
                    entry.get("title", ""),
                    text,
                    canonical_url(link),
                    published,
                    source["language_hint"],
                    [
                        {"url": e.get("href", ""), "mime": e.get("type", "")}
                        for e in entry.get("enclosures", [])
                        if e.get("type", "").startswith("video/")
                    ],
                )
            )
    # Обрабатываем ветвь при условии: source['type'] == 'html'.
    elif source["type"] == "html":
        from .config import inside, parse_json

        profile_path = inside(root, f"config/extraction/{source['extraction_profile']}.json")
        # Обрабатываем ветвь при условии: not profile_path.exists().
        if not profile_path.exists():
            raise AgentError("EXTRACTION_PROFILE_MISSING")
        profile = parse_json(profile_path.read_text(encoding="utf-8-sig"))
        soup = BeautifulSoup(content, "html.parser")
        body, title = (
            soup.select_one(profile["body_selector"]),
            soup.select_one(profile["title_selector"]),
        )
        # Обрабатываем ветвь при условии: not body or not title.
        if not body or not title:
            raise AgentError("ARTICLE_SELECTOR_NO_MATCH")
        published = None
        # Обрабатываем ветвь при условии: profile.get('date_selector').
        if profile.get("date_selector"):
            node = soup.select_one(profile["date_selector"])
            # Обрабатываем ветвь при условии: node and node.get('datetime').
            if node and node.get("datetime"):
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    dt = datetime.fromisoformat(node["datetime"].replace("Z", "+00:00"))
                    # Обрабатываем ветвь при условии: dt.tzinfo is None.
                    if dt.tzinfo is None:
                        raise ValueError
                    published = dt.astimezone(UTC).timestamp()
                # Обрабатываем ValueError и выбираем допустимый исход.
                except ValueError:
                    published = None
        items.append(
            Item(
                url,
                clean_html(str(title)),
                clean_html(str(body)),
                url,
                published,
                source["language_hint"],
            )
        )
    else:
        # Альтернативная ветвь после проверки основного условия.
        raise AgentError("UNSUPPORTED_SOURCE")
    return items, {
        "etag": returned_headers.get("etag"),
        "last_modified": returned_headers.get("last-modified"),
    }


# Преобразуем сообщения платформы в единый формат материалов.
def messenger_items(platform, payload, source):
    # Обрабатываем ветвь при условии: platform == 'telegram'.
    if platform == "telegram":
        message = payload.get("channel_post") or payload.get("edited_channel_post")
        # Обрабатываем ветвь при условии: not message or str(message.get('chat', {}).get('id')) != source['external_chat_id'].
        if not message or str(message.get("chat", {}).get("id")) != source["external_chat_id"]:
            return []
        text = message.get("text", message.get("caption", ""))
        mid = str(message["message_id"])
        username = message.get("chat", {}).get("username")
        link = f"https://t.me/{username}/{mid}" if username else ""
        return [
            Item(
                mid,
                text.split("\n")[0][:200],
                text,
                link,
                message.get("date"),
                detect_language(text),
            )
        ]
    message = payload.get("message", {})
    # Обрабатываем ветвь при условии: str(message.get('recipient', {}).get('chat_id')) != source['external_chat_id'].
    if str(message.get("recipient", {}).get("chat_id")) != source["external_chat_id"]:
        return []
    body = message.get("body", {})
    text = body.get("text", "")
    return [
        Item(
            str(body.get("mid", "")),
            text.split("\n")[0][:200],
            text,
            message.get("url", ""),
            message.get("timestamp", 0) / 1000 or None,
            detect_language(text),
        )
    ]
