"""Ограниченный официальный поиск NASA SVS без моделей, отправки или медиа."""

import hashlib
import importlib.util
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select

from .config import AgentError, fingerprint, inside
from .db import Cursor, Material
from .reddit import text_hash

API = "https://svs.gsfc.nasa.gov/api/"
MAX_BYTES = 2 * 1024 * 1024
RIGHTS = re.compile(r"copyright|all rights reserved|©|licensed music|music:", re.I)
CHECKPOINT_ID = "server_nasa_svs"


def _get(client, url, params=None):
    with client.stream("GET", url, params=params) as response:
        if response.status_code != 200:
            raise AgentError("NASA_SOURCE_UNAVAILABLE", 503)
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise AgentError("ARTICLE_SOURCE_TOO_LARGE")
    return json.loads(raw), bytes(raw)


def _parser():
    # Используем проверенный links-v1 extractor с сохранением видимых href.
    path = Path(__file__).resolve().parents[2] / "scripts/fetch_nasa_article.py"
    spec = importlib.util.spec_from_file_location("max_postiz_nasa_extractor", path)
    if spec is None or spec.loader is None:
        raise AgentError("NASA_EXTRACTOR_UNAVAILABLE", 503)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _grams(value):
    words = re.findall(r"[\w]+", value.casefold())
    return {tuple(words[i : i + 5]) for i in range(max(0, len(words) - 4))}


def _used_narratives(core, ledger):
    records = [r for r in ledger["records"] if r.get("original_hash")]
    with core.db.session() as session:
        materials = list(session.scalars(select(Material)))
        by_url = {m.url: m.text for m in materials if m.text}
    result = []
    for record in records:
        value = by_url.get(record.get("url"), "")
        if not value or text_hash(value) != record["original_hash"]:
            trace = inside(core.settings.root, record.get("quality_trace", ""))
            if not trace.is_file() or trace.stat().st_size > MAX_BYTES:
                raise AgentError("NASA_DEDUP_CORPUS_INCOMPLETE", 409)
            value = ""
            for line in trace.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                source = event.get("source_untrusted") or event.get("context", {}).get(
                    "source_untrusted"
                )
                if (
                    isinstance(source, dict)
                    and isinstance(source.get("body"), str)
                    and text_hash(source["body"]) == record["original_hash"]
                ):
                    value = source["body"]
                    break
        if not value or text_hash(value) != record["original_hash"]:
            raise AgentError("NASA_DEDUP_CORPUS_INCOMPLETE", 409)
        result.append(value)
    return result


def _exclusions(core):
    """Частные подтверждённые отказы источника действуют и после миграции."""
    path = inside(core.settings.root, "data/nasa-source-exclusions.local.json")
    if not path.exists():
        return set(), set()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = value["excluded"]
        if not isinstance(rows, list):
            raise ValueError
        urls, hashes = set(), set()
        for row in rows:
            if not isinstance(row, dict) or not re.fullmatch(
                r"https://svs\.gsfc\.nasa\.gov/[1-9][0-9]{0,6}/", row.get("url", "")
            ):
                raise ValueError
            urls.add(row["url"])
            if row.get("original_hash"):
                if not re.fullmatch(r"[a-f0-9]{64}", row["original_hash"]):
                    raise ValueError
                hashes.add(row["original_hash"])
        return urls, hashes
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AgentError("NASA_EXCLUSIONS_INVALID", 409) from exc


def _article(data, extractor, canonical):
    if data.get("page_type") != "Visualization" or data.get("url") not in {
        canonical,
        canonical.replace("https://", "http://", 1),
    }:
        raise AgentError("ARTICLE_SOURCE_METADATA_INVALID")
    sections = [
        g.get("description", "")
        for g in data.get("media_groups", [])
        if g.get("widget") in {"Basic text", "Basic text with HTML"} and g.get("description")
    ]
    if not sections:
        raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED")
    original = "\n\n".join(extractor.narrative_text(s, canonical) for s in sections)
    title = extractor.clean_html(data["title"])
    if not title or not 100 <= len(original) <= 7500:
        raise AgentError("ARTICLE_SOURCE_LENGTH_INVALID")
    # Проверяем credits и группы именно этой страницы, исключая чужие relations.
    # Сторонние исключения из public domain не снимаются автоматически.
    own = {
        k: data.get(k) for k in ("title", "description", "credits", "main_credits", "media_groups")
    }
    if RIGHTS.search(json.dumps(own, ensure_ascii=False)):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    if not any(
        isinstance(data.get(k), (dict, list)) and data[k] for k in ("credits", "main_credits")
    ):
        raise AgentError("ARTICLE_SOURCE_CREDITS_REQUIRED")
    date = datetime.fromisoformat(data["release_date"])
    if date.tzinfo is None:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID")
    return {
        "url": canonical,
        "title": title,
        "original": original,
        "published_at": date.timestamp(),
    }


def _checkpoint(core, selection_hash, enabled):
    value = {"offset": 0, "head_seen": [], "offset_seen": [], "selection_hash": selection_hash}
    if not enabled:
        return value
    with core.db.session() as session:
        row = session.get(Cursor, CHECKPOINT_ID)
        old = row.data if row else None
        if old and old.get("selection_hash") == selection_hash:
            if (
                set(old) != set(value)
                or type(old["offset"]) is not int
                or not 0 <= old["offset"] <= 10_000_000
                or any(
                    not isinstance(old[k], list)
                    or len(old[k]) > 100
                    or any(type(i) is not int or not 1 <= i <= 9999999 for i in old[k])
                    for k in ("head_seen", "offset_seen")
                )
            ):
                raise AgentError("NASA_CHECKPOINT_INVALID", 409)
            return {k: list(v) if isinstance(v, list) else v for k, v in old.items()}
    return value


def _save_checkpoint(core, value, enabled, stamp):
    if not enabled:
        return
    from .server_state import require_running

    with core.db.session.begin() as session:
        require_running(core, session)
        row = session.scalar(select(Cursor).where(Cursor.id == CHECKPOINT_ID).with_for_update())
        if row is None:
            row = Cursor(id=CHECKPOINT_ID)
            session.add(row)
        row.data = {k: list(v) if isinstance(v, list) else v for k, v in value.items()}
        row.status, row.last_success = "source_metadata_checkpoint", stamp


def _next_offset(catalog, current, limit):
    """Читаем только offset; URL из ответа никогда не становится адресом GET."""
    value = catalog.get("next")
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, strict_parsing=True)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != "svs.gsfc.nasa.gov"
            or parsed.port is not None
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.path != "/api/search/"
            or set(query) - {"studio", "limit", "offset"}
            or any(len(v) != 1 for v in query.values())
            or query.get("studio") != ["SVS"]
            or query.get("limit") != [str(limit)]
            or not re.fullmatch(r"[0-9]+", query["offset"][0])
        ):
            raise ValueError
        offset = int(query["offset"][0])
        if not current < offset <= 10_000_000:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError) as exc:
        raise AgentError("NASA_SOURCE_CATALOG_INVALID", 503) from exc


def select_nasa_article(core, options, ledger, policy, *, now, transport=None, checkpoint=False):
    """Один полный свежий текст или None; максимум три каталога и N полных страниц."""
    if options["min_rating"]:
        # У SVS нет подтверждённой шкалы популярности: рейтинг не выдумываем.
        raise AgentError("NASA_RATING_UNAVAILABLE", 409)
    if (
        policy.get("source_id") != "nasa_svs_articles"
        or policy.get("allowed_hosts") != ["svs.gsfc.nasa.gov"]
        or policy.get("rights_evidence") != "https://svs.gsfc.nasa.gov/help/"
    ):
        raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
    source = core.settings.source(policy["source_id"])
    rights = source["rights"]
    if not (
        source["enabled"]
        and rights["verified"]
        and rights["allow_storage"]
        and rights["allow_derivative"]
        and rights["evidence_ref"] == policy["rights_evidence"]
    ):
        raise AgentError("ARTICLE_RIGHTS_REQUIRED", 409)
    used_urls = {r.get("url") for r in ledger["records"]}
    used_hashes = {r.get("original_hash") for r in ledger["records"]}
    excluded_urls, excluded_hashes = _exclusions(core)
    used_urls.update(excluded_urls)
    used_hashes.update(excluded_hashes)
    existing = _used_narratives(core, ledger)
    extractor = _parser()
    limit = options["max_candidates"]
    progress = _checkpoint(core, fingerprint({"options": options, "policy": policy}), checkpoint)
    next_page, full_gets = 0, 0
    with httpx.Client(
        timeout=httpx.Timeout(30, connect=10),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as client:
        for _page_number in range(3):
            offset = next_page
            params = {"studio": "SVS", "limit": limit}
            if offset:
                params["offset"] = offset
            catalog, _ = _get(client, API + "search/", params)
            results = catalog.get("results") if isinstance(catalog, dict) else None
            if not isinstance(results, list) or len(results) > limit:
                raise AgentError("NASA_SOURCE_CATALOG_INVALID", 503)
            following = _next_offset(catalog, offset, limit)
            seen_key = "head_seen" if offset == 0 else "offset_seen"
            if offset and progress["offset"] != offset:
                progress.update(offset=offset, offset_seen=[])
            current_ids = {
                r.get("id") for r in results if isinstance(r, dict) and type(r.get("id")) is int
            }
            progress[seen_key] = [i for i in progress[seen_key] if i in current_ids]

            def remember(ident, seen_key=seen_key):
                if ident not in progress[seen_key]:
                    progress[seen_key].append(ident)
                _save_checkpoint(core, progress, checkpoint, now)

            for item in results:
                # Search API может не содержать тип; авторитетен полный Page API.
                if (
                    not isinstance(item, dict)
                    or item.get("page_type") is not None
                    and item["page_type"] != "Visualization"
                ):
                    continue
                ident = item.get("id")
                if type(ident) is not int or not 1 <= ident <= 9999999:
                    continue
                if ident in progress[seen_key]:
                    continue
                canonical = f"https://svs.gsfc.nasa.gov/{ident}/"
                if canonical in used_urls:
                    remember(ident)
                    continue
                try:
                    released = datetime.fromisoformat(item["release_date"])
                    # Будущая дата временна: не исключаем материал из последующего подбора.
                    if released.tzinfo is not None and released.timestamp() > now:
                        continue
                    if (
                        released.tzinfo is None
                        or not 0
                        <= now - released.timestamp()
                        <= policy["max_source_age_hours"] * 3600
                    ):
                        remember(ident)
                        continue
                    if full_gets >= limit:
                        _save_checkpoint(core, progress, checkpoint, now)
                        return None
                    full_gets += 1
                    data, raw = _get(client, API + str(ident) + "/")
                    full_released = datetime.fromisoformat(data["release_date"])
                    if full_released.tzinfo is not None and full_released.timestamp() > now:
                        continue
                    article = _article(data, extractor, canonical)
                    if (
                        not 0
                        <= now - article["published_at"]
                        <= policy["max_source_age_hours"] * 3600
                    ):
                        remember(ident)
                        continue
                    text = article["original"]
                    if (
                        len(text.split()) > options["max_words"]
                        or text_hash(text) in used_hashes
                        or options["keywords"]
                        and not any(
                            k.casefold() in (article["title"] + " " + text).casefold()
                            for k in options["keywords"]
                        )
                    ):
                        remember(ident)
                        continue
                    if options["authors"]:
                        authors = json.dumps(
                            data.get("main_credits", data.get("credits", {})), ensure_ascii=False
                        ).casefold()
                        if not any(a.casefold() in authors for a in options["authors"]):
                            remember(ident)
                            continue
                    grams = _grams(text)
                    if grams and any(
                        len(grams & _grams(old)) / len(grams) >= 0.40 for old in existing
                    ):
                        remember(ident)
                        continue
                except AgentError as exc:
                    if exc.status >= 500:
                        raise
                    remember(ident)
                    continue
                except (KeyError, TypeError, ValueError):
                    remember(ident)
                    continue
                directory = inside(core.settings.root, "data/server-sources")
                directory.mkdir(parents=True, exist_ok=True)
                key = hashlib.sha256(raw).hexdigest()
                proof = {
                    "api_url": API + str(ident) + "/",
                    "api_sha256": key,
                    "article": article,
                    "page": data,
                    "rights_evidence": policy["rights_evidence"],
                    "selected_at": now,
                    "dedup_records": len(ledger["records"]),
                }
                path = directory / (str(ident) + "-" + key + ".local.json")
                if not path.exists():
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(proof, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                remember(ident)
                return article
            # Page0 проверяется каждый раз; затем продолжаем прежнюю позицию каталога.
            if offset == 0:
                next_page = progress["offset"] if progress["offset"] >= limit else following
            else:
                next_page = following
            if next_page is None:
                progress.update(offset=0, offset_seen=[])
                _save_checkpoint(core, progress, checkpoint, now)
                return None
            if next_page != progress["offset"]:
                progress.update(offset=next_page, offset_seen=[])
            _save_checkpoint(core, progress, checkpoint, now)
    return None
