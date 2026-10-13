"""JSON API ingestion with bounded pagination, restartable offsets and header credentials."""
import json
import os
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .collectors import Item, clean_html
from .config import AgentError
from .network import canonical_url


def field(value, path, default=None):
    for key in path.split(".") if path else []:
        if isinstance(value, dict):
            value = value.get(key, default)
        else:
            return default
    return value


def with_param(url, name, value):
    p = urlsplit(url)
    values = dict(parse_qsl(p.query))
    values[name] = str(value)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(values), ""))


def collect_json(source, cursor, http, root):
    config = source["api"]
    headers = {"Accept": "application/json"}
    if source.get("access_ref"):
        secret = os.getenv(source["access_ref"])
        if not secret:
            raise AgentError("SOURCE_ACCESS_REQUIRED")
        headers[config.get("auth_header", "Authorization")] = config.get("auth_prefix", "Bearer ") + secret
    url = cursor.get("api_url") or source["endpoint"]
    origin = urlsplit(source["endpoint"])
    offset = cursor.get("api_offset", 0)
    seen, items = set(), []
    limit = source["max_items_per_run"]
    for _ in range(config.get("max_pages", 5)):
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise AgentError("SOURCE_PAGINATION_ORIGIN_CHANGED")
        if url in seen:
            raise AgentError("SOURCE_PAGINATION_LOOP")
        seen.add(url)
        _, raw, _, actual = http.get(url, headers)
        actual_origin = urlsplit(actual)
        if (actual_origin.scheme, actual_origin.netloc) != (origin.scheme, origin.netloc):
            raise AgentError("SOURCE_PAGINATION_ORIGIN_CHANGED")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AgentError("SOURCE_JSON_INVALID") from exc
        rows = field(data, config["items_path"])
        if not isinstance(rows, list):
            raise AgentError("SOURCE_ITEMS_INVALID")
        for index, row in enumerate(rows[offset:], start=offset):
            if len(items) >= limit:
                return items, {"api_url": url, "api_offset": index}
            fields = config["fields"]
            ident, body = field(row, fields["id"]), field(row, fields["text"])
            if not isinstance(ident, (str, int)) or not isinstance(body, str):
                raise AgentError("SOURCE_ITEM_INVALID")
            link = field(row, fields["url"], "")
            if not isinstance(link, str) or not link:
                raise AgentError("SOURCE_ITEM_URL_REQUIRED")
            link = canonical_url(urljoin(source["endpoint"], link))
            published = field(row, fields.get("published_at", ""))
            stamp = None
            if published is not None:
                try:
                    dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        raise ValueError
                    stamp = dt.astimezone(UTC).timestamp()
                except ValueError as exc:
                    raise AgentError("SOURCE_ITEM_DATE_INVALID") from exc
            title = field(row, fields.get("title", ""), "")
            if not isinstance(title, str):
                raise AgentError("SOURCE_ITEM_INVALID")
            items.append(Item(str(ident), title, clean_html(body), link, stamp, source["language_hint"]))
        offset = 0
        next_value = field(data, config.get("next_path", "")) if config.get("next_path") else None
        if next_value in (None, "", False):
            return items, {"api_url": source["endpoint"], "api_offset": 0}
        if not isinstance(next_value, (str, int)) or isinstance(next_value, bool):
            raise AgentError("SOURCE_CURSOR_INVALID")
        url = (with_param(source["endpoint"], config["cursor_param"], next_value)
               if config.get("cursor_param") else urljoin(url, str(next_value)))
    return items, {"api_url": url, "api_offset": 0}
