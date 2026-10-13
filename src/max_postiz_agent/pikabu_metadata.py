"""Metadata and story URL contracts reused from the existing Pikabu counterpart of MAX."""
import re
from urllib.parse import urlsplit

from .config import AgentError


def story_url(value):
    if not isinstance(value, str):
        raise AgentError("PIKABU_STORY_URL_INVALID")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise AgentError("PIKABU_STORY_URL_INVALID") from exc
    if parsed.scheme != "https" or parsed.netloc not in {"pikabu.ru", "www.pikabu.ru"} or parsed.query or parsed.fragment or not re.fullmatch(r"/story/[A-Za-z0-9_-]+_\d+", parsed.path):
        raise AgentError("PIKABU_STORY_URL_INVALID")
    return "https://pikabu.ru" + parsed.path


def metadata(title, values=None):
    values = values or {}
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise AgentError("PIKABU_TITLE_INVALID")
    tags = values.get("tags", ["Нейросети", "Контент нейросетей"])
    if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or not t.strip() or len(t) > 50 for t in tags):
        raise AgentError("PIKABU_TAGS_INVALID")
    tags = [t.strip() for t in tags]
    if len({t.casefold() for t in tags}) != len(tags):
        raise AgentError("PIKABU_TAGS_INVALID")
    if "контент нейросетей" not in {t.casefold() for t in tags}:
        tags.append("Контент нейросетей")
    if len(tags) > 10:
        raise AgentError("PIKABU_TAGS_INVALID")
    community = values.get("community", "")
    if not isinstance(community, str) or len(community) > 200:
        raise AgentError("PIKABU_COMMUNITY_INVALID")
    return {"tags": tags, "community": community, "ai_generated": True}
