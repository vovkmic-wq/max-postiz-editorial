"""Ограниченный сбор полных научных текстов с явной лицензией CC BY 4.0.

Картинки, видео и ссылки из повествования не загружаются. Все исходные HTML,
credits, условия лицензии и literal href остаются в закрытом доказательстве.
"""

import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy import select

from .article_sources import source_spec, validate_source_url
from .config import AgentError, fingerprint, inside
from .db import Cursor
from .reddit import text_hash
from .server_nasa import MAX_BYTES, _exclusions, _grams, _parser, _used_narratives

ADAPTERS = {
    "noirlab_iotw": {
        "catalog": "/public/images/iotw/",
        "pages": r"/public/images/iotw/page/([1-9][0-9]{0,3})/",
        "kind": "Image of the Week",
        "publisher": "NOIRLab",
        "rights_labels": {"Usage of NOIRLab Images and Videos", "Copyright"},
        "newsletter_url": "https://noirlab.edu/public/subscribe-media/",
        "newsletter_label": "Are you a journalist? Subscribe to the NOIRLab Media Newsletter.",
    },
    "esahubble_potw": {
        "catalog": "/images/potw/",
        "pages": r"/images/potw/page/([1-9][0-9]{0,3})/",
        "kind": "Picture of the Week",
        "publisher": "ESA/Hubble",
        "rights_labels": {"Usage of ESA/Hubble Images and Videos", "Copyright"},
        "newsletter_url": "https://esahubble.org/press/mailinglist",
        "newsletter_label": "Are you a journalist? Subscribe to the ESA/Hubble Media Newsletter.",
    },
}
MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
EXCEPTION = re.compile(
    # Частный запрет в credits относится к этой странице, даже если общий
    # copyright сайта разрешает CC BY. Не ограничиваемся словом copyright:
    # NC, ND, personal-use и прямой запрет перевода тоже блокируют отбор.
    r"copyright|all\s+rights\s+reserved|©|not (?:licensed|released)|used with permission|"
    r"\bpersonal[\s\u2010-\u2015-]+use[\s\u2010-\u2015-]+only\b|"
    r"\bonly\s+(?:for\s+)?personal[\s\u2010-\u2015-]+use\b|"
    r"\bnon[\s\u2010-\u2015-]*commercial\b|"
    r"\bno[\s\u2010-\u2015-]*(?:derivatives?|adaptations?|translations?|reproductions?|redistributions?)\b|"
    r"\bcc[\s\u2010-\u2015-]*by[\s\u2010-\u2015-]+(?:nc|nd)\b|"
    r"\b(?:derivatives?|adaptations?|translations?|reproductions?|redistributions?|re[\s-]?use)\s+"
    r"(?:(?:is|are)\s+)?(?:not\s+(?:permitted|allowed|authori[sz]ed)|(?:strictly\s+)?prohibited|forbidden|restricted)\b|"
    r"\bnot\s+(?:permitted|allowed)(?:\s*:\s*|\s+)(?:translations?|adaptations?|reproductions?|redistributions?|reuse)\b|"
    r"\b(?:do not|must not)\s+(?:translate|reproduce|redistribute|reuse|adapt|modify)\b|"
    r"\b(?:(?:separate|prior|written)\s+)?permission\s+(?:is\s+)?required\b|"
    r"\b(?:translation|reproduction|reuse|adaptation)\s+requires\s+(?:(?:separate|prior|written)\s+)*permission\b",
    re.I,
)
ACCESS_CHALLENGE = re.compile(
    rb"not a robot|verify (?:that )?you are (?:a )?human|javascript is disabled|"
    rb"enable javascript.{0,80}(?:continue|reload)|cf-chl-|challenge-platform|g-recaptcha|h-captcha",
    re.I,
)


def _html(raw):
    """Дубли атрибутов не должны скрыть первый опасный href после разбора HTML."""

    def duplicate(_attrs, _name, _value):
        raise AgentError("ARTICLE_SOURCE_HTML_INVALID")

    try:
        return BeautifulSoup(raw.decode("utf-8"), "html.parser", on_duplicate_attribute=duplicate)
    except UnicodeError as exc:
        raise AgentError("ARTICLE_SOURCE_HTML_INVALID") from exc


def _get(client, url):
    try:
        with client.stream("GET", url) as response:
            if response.status_code in {401, 403, 429}:
                raise AgentError("SCIENCE_SOURCE_ACCESS_BLOCKED", 503)
            if response.status_code != 200:
                raise AgentError("SCIENCE_SOURCE_UNAVAILABLE", 503)
            if response.headers.get("content-type", "").split(";", 1)[0] not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise AgentError("SCIENCE_SOURCE_CONTENT_INVALID", 503)
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise AgentError("ARTICLE_SOURCE_TOO_LARGE")
            if ACCESS_CHALLENGE.search(raw):
                raise AgentError("SCIENCE_SOURCE_ACCESS_BLOCKED", 503)
            return bytes(raw)
    except httpx.HTTPError as exc:
        # Не повторяем 429, challenge, timeout или неизвестный сетевой исход.
        raise AgentError("SCIENCE_SOURCE_UNAVAILABLE", 503) from exc


def _day(value):
    match = re.fullmatch(r"([0-9]{1,2}) ([A-Za-z]+) ([0-9]{4})", value)
    if not match or match[2] not in MONTHS:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID")
    try:
        return datetime(int(match[3]), MONTHS[match[2]], int(match[1]), tzinfo=UTC)
    except ValueError as exc:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID") from exc


def _release_day(value):
    # Сайты не указывают timezone. Используем только опубликованный календарный
    # день: возраст считаем от его начала, а будущую публикацию — до его конца.
    match = re.fullmatch(r"([0-9]{1,2} [A-Za-z]+ [0-9]{4}), [0-9]{2}:[0-9]{2}", value)
    if match:
        return _day(match[1])
    match = re.fullmatch(
        r"([A-Za-z]+)\.? ([0-9]{1,2}), ([0-9]{4}), (?:noon|midnight|[0-9]{1,2}(?::[0-9]{2})? [ap]\.m\.)",
        value,
    )
    if not match:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID")
    month = next((name for name in MONTHS if name.startswith(match[1])), None)
    if month is None:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID")
    return _day(f"{match[2]} {month} {match[3]}")


def _unsupported_image_card(spec, literal, url):
    """Отсеять чистую ссылку другого image-раздела, не разрешая её загрузку."""
    parsed = urlsplit(url)
    prefix = "/public/images/" if spec["id"] == "noirlab_iotw" else "/images/"
    return (parsed.scheme == "https" and parsed.netloc == spec["host"]
        and not parsed.query and not parsed.fragment
        and url == "https://" + spec["host"] + parsed.path
        and not any(part in {".", ".."} for part in urlsplit(literal).path.split("/"))
        and re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/", parsed.path) is not None)


def _catalog(raw, source_id, page, rejected=None):
    spec, adapter = source_spec(source_id), ADAPTERS[source_id]
    soup = _html(raw)
    if soup.html is None or soup.html.get("lang", "").split("-", 1)[0] != "en":
        raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
    groups = soup.select(".news-list")
    if not groups:
        raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
    items, unique = [], set()
    for group in groups:
        for card in group.select("a[href]"):
            ident = card.select_one(".news-id")
            if ident is None or adapter["kind"] not in ident.get_text(" ", strip=True):
                continue
            literal = card["href"]
            if re.search(r"[\x00-\x20\x7f\\]", literal):
                raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
            url = urljoin("https://" + spec["host"], literal)
            title, date = card.select_one(".news-title"), card.select_one(".news-teaser strong")
            if (
                title is None
                or date is None
                or not title.get_text(" ", strip=True)
                or url in unique
            ):
                raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
            unique.add(url)
            day = _day(date.get_text(" ", strip=True))
            try:
                validate_source_url(source_id, url)
            except AgentError as exc:
                if exc.code != "ARTICLE_SOURCE_URL_INVALID":
                    raise
                if not _unsupported_image_card(spec, literal, url):
                    raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503) from exc
                # Это только отказ карточки. Точный whitelist статьи/прав не
                # меняется, detail/медиа по этой ссылке никогда не запрашиваются.
                if rejected is not None:
                    rejected.append({"literal_href": literal, "url": url,
                        "reason": "ARTICLE_SOURCE_URL_INVALID", "classification": "unsupported_image_card"})
                continue
            items.append(
                {
                    "url": url,
                    "title": title.get_text(" ", strip=True),
                    "day": day,
                }
            )
    if len(unique) > 100:
        raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
    following = []
    for link in soup.select("ul.pagination a[href], .prev-next a[href]"):
        if link.get_text(" ", strip=True).casefold() != "next":
            continue
        if re.search(r"[\x00-\x20\x7f\\]", link["href"]):
            raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
        parsed = urlsplit(urljoin("https://" + spec["host"], link["href"]))
        match = re.fullmatch(adapter["pages"], parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.netloc != spec["host"]
            or parsed.query
            or parsed.fragment
            or not match
            or int(match[1]) != page + 1
        ):
            raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
        following.append(int(match[1]))
    if len(following) > 1:
        raise AgentError("SCIENCE_SOURCE_CATALOG_INVALID", 503)
    return items, following[0] if following else None


def _catalog_rejection_proof(core, source_id, page, raw, rejected):
    """Сохранить исходный HTML и причины отказа карточек до следующего GET."""
    if not rejected:
        return
    hashed = hashlib.sha256(raw).hexdigest()
    spec, adapter = source_spec(source_id), ADAPTERS[source_id]
    catalog_path = adapter["catalog"] if page == 1 else adapter["catalog"] + f"page/{page}/"
    proof = {"contract": "science-catalog-rejections-v1", "source_id": source_id,
        "page": page, "catalog_url": "https://" + spec["host"] + catalog_path,
        "catalog_sha256": hashed, "rejected": rejected, "catalog_html": raw.decode("utf-8")}
    directory = inside(core.settings.root, "data/server-sources")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("catalog-rejections-" + source_id + "-" + hashed + ".local.json")
    if path.exists() or path.is_symlink():
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate catalog evidence key")
                result[key] = value
            return result
        try:
            if path.is_symlink() or path.stat().st_size > 20 * MAX_BYTES:
                raise ValueError("invalid catalog evidence file")
            old = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            if old != proof:
                raise ValueError("changed catalog evidence")
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise AgentError("SCIENCE_CATALOG_REJECTION_PROOF_CONFLICT", 409) from exc
        return
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(proof, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def _table_value(soup, label):
    values = []
    for row in soup.select(".right-column tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) == 2 and cells[0].get_text(" ", strip=True) == label:
            values.append(cells[1].get_text(" ", strip=True))
    if len(values) != 1 or not values[0]:
        raise AgentError("ARTICLE_SOURCE_METADATA_INVALID")
    return values[0]


def _article(raw, source_id, item, extractor):
    spec = source_spec(source_id)
    soup = _html(raw)
    columns = soup.select(".left-column")
    if len(columns) != 1 or soup.html is None or soup.html.get("lang", "").split("-", 1)[0] != "en":
        raise AgentError("ARTICLE_SOURCE_METADATA_INVALID")
    column = columns[0]
    titles = column.find_all("h1", recursive=False)
    if len(titles) != 1 or titles[0].get_text(" ", strip=True) != item["title"]:
        raise AgentError("ARTICLE_SOURCE_METADATA_INVALID")
    ident = urlsplit(item["url"]).path.rstrip("/").rsplit("/", 1)[1]
    if _table_value(soup, "Id:") != ident:
        raise AgentError("ARTICLE_SOURCE_METADATA_INVALID")
    literal_date = _table_value(soup, "Release date:")
    day = _release_day(literal_date)
    if day != item["day"]:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID")
    sections, credit_marker = [], None
    for node in titles[0].next_siblings:
        if not isinstance(node, Tag):
            if str(node).strip():
                raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED")
            continue
        if node.name == "strong" and node.get_text(" ", strip=True) == "Credit:":
            credit_marker = node
            break
        if node.name == "div" and "archive-image" in node.get("class", []):
            continue
        if source_id == "noirlab_iotw" and node.name == "p":
            sections.append(str(node))
        elif (
            source_id == "esahubble_potw"
            and node.name == "div"
            and set(node.attrs) == {"style"}
            and node.get("style") == "margin-top: 10px"
            and node.find("p", recursive=False)
        ):
            # Полный текст включает Image description и авторский Links; это
            # часть оригинала. Ни одна указанная в нём ссылка не запрашивается.
            sections.append(str(node))
        else:
            raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED")
    credits = column.select("div.credit")
    if (
        credit_marker is None
        or len(credits) != 1
        or not credits[0].get_text(" ", strip=True)
        or not sections
        or any(
            BeautifulSoup(s, "html.parser").find(["script", "iframe", "object", "video", "audio"])
            for s in sections
        )
    ):
        raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED")
    rights_boxes = column.select("div.copyright")
    if (
        len(rights_boxes) != 1
        or not rights_boxes[0].get_text(" ", strip=True)
        or rights_boxes[0].find(["script", "iframe", "object", "video", "audio"])
    ):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    # Проверенные два шаблона после Credit содержат ровно два собственных
    # служебных блока в этом порядке. Незнакомое примечание между/после ними
    # нельзя молча принять за внешний footer и потерять ограничение прав.
    tail, position = (credits[0], rights_boxes[0]), 0
    if credit_marker.attrs or credits[0].attrs != {"class": ["credit"]}:
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    for node in credit_marker.next_siblings:
        if not isinstance(node, Tag):
            if str(node).strip():
                raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
            continue
        if node.name == "br" and not node.attrs:
            continue
        if position >= len(tail) or node is not tail[position]:
            raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
        position += 1
    if position != len(tail):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    rights_box_html = str(rights_boxes[0])
    # Проверяем все href собственного блока прав; подпись «Copyright» сама по
    # себе допустима только на точной проверенной ссылке и с известным текстом.
    # Запреты рядом с ней или в изменённой подписи никогда не удаляются.
    extractor.narrative_text(rights_box_html, item["url"])
    rights_box = BeautifulSoup(rights_box_html, "html.parser")
    rights_links = []
    for anchor in rights_box.find_all("a"):
        if extractor._narrative_href(anchor["href"], item["url"]) == spec["rights_evidence"]:
            rights_links.append(anchor)
    if len(rights_links) != 1:
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    if not (
        rights_links[0].get_text(" ", strip=True) in ADAPTERS[source_id]["rights_labels"]
        and set(rights_links[0].attrs) == {"href"}
        and not rights_links[0].find(True)
    ):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    rights_links[0].decompose()
    for anchor in rights_box.find_all("a"):
        if not (
            set(anchor.attrs) == {"href"}
            and not anchor.find(True)
            and anchor.get_text(" ", strip=True) == ADAPTERS[source_id]["newsletter_label"]
            and extractor._narrative_href(anchor["href"], item["url"])
            == ADAPTERS[source_id]["newsletter_url"]
        ):
            raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
        anchor.decompose()
    # В проверенных шаблонах после двух точных служебных ссылок нет notice.
    # Любой новый текст требует разбора прав, включая personal use only и ND.
    if any(str(value).strip() for value in rights_box.find_all(string=True)):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    for tag in rights_box.find_all(True):
        if not (
            (tag.name == "div" and tag.attrs == {"class": ["copyright"]})
            or tag.name == "br"
            and not tag.attrs
        ):
            raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    text = "\n\n".join(extractor.narrative_text(s, item["url"]) for s in sections)
    credit = credits[0].get_text(" ", strip=True)
    if not 100 <= len(text) <= 7500:
        raise AgentError("ARTICLE_SOURCE_LENGTH_INVALID")
    if EXCEPTION.search(text + " " + credit):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    links = []
    for section in sections:
        part = BeautifulSoup(section, "html.parser")
        links.extend(
            {
                "label": a.get_text(" ", strip=True),
                "url": extractor._narrative_href(a["href"], item["url"]),
            }
            for a in part.find_all("a")
        )
    article = {
        "url": item["url"],
        "title": item["title"],
        "original": text,
        "published_at": day.timestamp(),
    }
    return article, {
        "credit": credit,
        "narrative_html": sections,
        "links": links,
        "release_date_literal": literal_date,
        "date_precision": "day",
        "rights_box_html": rights_box_html,
        "rights_box_text": rights_boxes[0].get_text(" ", strip=True),
    }


def _rights(raw, source_id):
    adapter = ADAPTERS[source_id]
    spec = source_spec(source_id)
    soup = _html(raw)
    blocks = soup.select("#editable-content")
    if len(blocks) != 1:
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    block = blocks[0]
    # Проверены полные условия, а не ссылка на лицензию в общей навигации.
    # Изменение смыслового текста требует отдельной проверки прав; стили,
    # переносы строк и остальная навигация не меняют договорённость.
    text = " ".join(block.get_text(" ", strip=True).split())
    semantic_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    hrefs = {a.get("href") for a in block.find_all("a")}
    if (
        not (
            {
                "https://creativecommons.org/licenses/by/4.0/",
                "http://creativecommons.org/licenses/by/4.0/",
            }
            & hrefs
        )
        or "creative commons attribution 4.0" not in text.casefold()
        or adapter["publisher"].casefold() not in text.casefold()
        or semantic_hash != spec["rights_text_sha256"]
    ):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")


def _checkpoint(core, source_id, selected_hash, enabled):
    value = {"page": 1, "head_seen": [], "page_seen": [], "selection_hash": selected_hash}
    if not enabled:
        return value
    with core.db.session() as session:
        row = session.get(Cursor, "server_science_" + source_id)
        if row is not None and row.status == "access_blocked":
            # Смена фильтра не является разрешением повторять запрос к ресурсу,
            # который явно запретил доступ или потребовал проверку человека.
            raise AgentError("SCIENCE_SOURCE_ACCESS_BLOCKED", 503)
        old = row.data if row else None
    if old is not None and not isinstance(old, dict):
        raise AgentError("SCIENCE_CHECKPOINT_INVALID", 409)
    if old and old.get("selection_hash") == selected_hash:
        if (
            set(old) != set(value)
            or type(old["page"]) is not int
            or not 1 <= old["page"] <= 9999
            or any(
                not isinstance(old[k], list)
                or len(old[k]) > 100
                or any(not isinstance(v, str) for v in old[k])
                or len(set(old[k])) != len(old[k])
                for k in ("head_seen", "page_seen")
            )
        ):
            raise AgentError("SCIENCE_CHECKPOINT_INVALID", 409)
        for key in ("head_seen", "page_seen"):
            for url in old[key]:
                validate_source_url(source_id, url)
        return {
            key: list(value) if isinstance(value, list) else value for key, value in old.items()
        }
    return value


def _save_checkpoint(core, source_id, progress, enabled, stamp):
    if not enabled:
        return
    from .server_state import require_running

    with core.db.session.begin() as session:
        require_running(core, session)
        row = session.scalar(
            select(Cursor).where(Cursor.id == "server_science_" + source_id).with_for_update()
        )
        if row is None:
            row = Cursor(id="server_science_" + source_id)
            session.add(row)
        row.data = {
            key: list(value) if isinstance(value, list) else value
            for key, value in progress.items()
        }
        row.status, row.last_success = "source_metadata_checkpoint", stamp


def _mark_access_blocked(core, source_id, progress, enabled):
    if not enabled:
        return
    from .server_state import require_running

    with core.db.session.begin() as session:
        require_running(core, session)
        row = session.scalar(
            select(Cursor).where(Cursor.id == "server_science_" + source_id).with_for_update()
        )
        if row is None:
            row = Cursor(
                id="server_science_" + source_id,
                data={
                    key: list(value) if isinstance(value, list) else value
                    for key, value in progress.items()
                },
            )
            session.add(row)
        # Не сбрасываем позицию, исключения или прежний last_success. Для
        # возобновления требуется адресная сверка доступа вне обычного цикла.
        row.status = "access_blocked"


def _proof(
    core, source_id, article, details, page_raw, catalog_raw, rights_raw, policy, now, count
):
    hashes = {
        key: hashlib.sha256(raw).hexdigest()
        for key, raw in (("page", page_raw), ("catalog", catalog_raw), ("rights", rights_raw))
    }
    proof = {
        "source_id": source_id,
        "article": article,
        **details,
        "hashes": hashes,
        "page_html": page_raw.decode("utf-8"),
        "catalog_html": catalog_raw.decode("utf-8"),
        "rights_html": rights_raw.decode("utf-8"),
        "rights_evidence": policy["rights_evidence"],
        "rights_text_sha256": source_spec(source_id)["rights_text_sha256"],
        "selected_at": now,
        "dedup_records": count,
    }
    directory = inside(core.settings.root, "data/server-sources")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (source_id + "-" + fingerprint(hashes) + ".local.json")
    if path.exists():

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate proof key")
                result[key] = value
            return result

        try:
            # Три bounded HTML могут разрастись при JSON-экранировании до 6x.
            if path.is_symlink() or path.stat().st_size > 20 * MAX_BYTES:
                raise ValueError("invalid proof file")
            old = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            valid = (
                isinstance(old, dict)
                and set(old) == set(proof)
                and type(old["selected_at"]) in {int, float}
                and math.isfinite(old["selected_at"])
                and 0 <= old["selected_at"] <= now
                and type(old["dedup_records"]) is int
                and 0 <= old["dedup_records"] <= count
                and all(
                    old[key] == value
                    for key, value in proof.items()
                    if key not in {"selected_at", "dedup_records"}
                )
            )
        except (OSError, ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise AgentError("SCIENCE_SOURCE_PROOF_CONFLICT", 409) from exc
        if not valid:
            raise AgentError("SCIENCE_SOURCE_PROOF_CONFLICT", 409)
        return
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(proof, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def select_science_article(
    core, source_id, options, ledger, policy, *, now, transport=None, checkpoint=False
):
    """Один полный новый текст; не более трёх каталогов и N полных страниц."""
    spec = source_spec(source_id)
    if options["min_rating"]:
        raise AgentError("SCIENCE_RATING_UNAVAILABLE", 409)
    if (
        policy.get("source_id") != source_id
        or policy.get("allowed_hosts") != [spec["host"]]
        or policy.get("rights_evidence") != spec["rights_evidence"]
    ):
        raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
    source = core.settings.source(source_id)
    rights = source.get("rights", {})
    if (
        source.get("enabled") is not True
        or rights.get("verified") is not True
        or rights.get("allow_storage") is not True
        or rights.get("allow_derivative") is not True
        or rights.get("allow_full_repost") is not True
        or rights.get("evidence_ref") != spec["rights_evidence"]
    ):
        raise AgentError("ARTICLE_RIGHTS_REQUIRED", 409)
    used_urls = {r.get("url") for r in ledger["records"]}
    used_hashes = {r.get("original_hash") for r in ledger["records"]}
    # Подтверждённый старый отказ нельзя обойти зеркалом на новом домене.
    # Формат и проверки частного NASA-журнала исключений остаются прежними.
    excluded_urls, excluded_hashes = _exclusions(core)
    used_urls.update(excluded_urls)
    used_hashes.update(excluded_hashes)
    # Используем тот же полный корпус NASA/других источников, включая отмены.
    existing = _used_narratives(core, ledger)
    progress = _checkpoint(
        core, source_id, fingerprint({"options": options, "policy": policy}), checkpoint
    )
    adapter, extractor = ADAPTERS[source_id], _parser()
    next_page, full_gets, rights_raw = 1, 0, None
    with httpx.Client(
        # Большой официальный HTML иногда приходит медленно: чтение ограничено
        # 60 секундами, подключение — прежними 10. Повтор запроса не добавляем.
        timeout=httpx.Timeout(30, connect=10, read=60),
        # В этой инфраструктуре повторное использование HTTP-соединения может
        # зависать после каталога. Следующие GET независимы; запросы не повторяем.
        limits=httpx.Limits(max_keepalive_connections=0),
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as client:

        def get(url):
            try:
                return _get(client, url)
            except AgentError as exc:
                if exc.code == "SCIENCE_SOURCE_ACCESS_BLOCKED":
                    _mark_access_blocked(core, source_id, progress, checkpoint)
                raise

        for _ in range(3):
            page = next_page
            path = adapter["catalog"] if page == 1 else adapter["catalog"] + f"page/{page}/"
            catalog_raw = get("https://" + spec["host"] + path)
            rejected_cards = []
            items, following = _catalog(catalog_raw, source_id, page, rejected=rejected_cards)
            _catalog_rejection_proof(core, source_id, page, catalog_raw, rejected_cards)
            seen_key = "head_seen" if page == 1 else "page_seen"
            if page > 1 and progress["page"] != page:
                progress.update(page=page, page_seen=[])
            current_urls = {item["url"] for item in items}
            progress[seen_key] = [u for u in progress[seen_key] if u in current_urls]

            def remember(url, key=seen_key):
                if url not in progress[key]:
                    progress[key].append(url)
                _save_checkpoint(core, source_id, progress, checkpoint, now)

            for item in items:
                url, day = item["url"], item["day"]
                if url in progress[seen_key]:
                    continue
                if url in used_urls:
                    remember(url)
                    continue
                if day.timestamp() + 86400 > now:
                    continue
                if now - day.timestamp() > policy["max_source_age_hours"] * 3600:
                    remember(url)
                    continue
                if full_gets >= options["max_candidates"]:
                    _save_checkpoint(core, source_id, progress, checkpoint, now)
                    return None
                full_gets += 1
                page_raw = get(url)
                try:
                    full_day = _release_day(_table_value(_html(page_raw), "Release date:"))
                    if full_day.timestamp() + 86400 > now:
                        # Исправленная позже дата не должна навсегда потерять
                        # статью даже при другой ошибке её будущего HTML.
                        continue
                    article, details = _article(page_raw, source_id, item, extractor)
                    text = article["original"]
                    if (
                        len(text.split()) > options["max_words"]
                        or text_hash(text) in used_hashes
                        or options["keywords"]
                        and not any(
                            k.casefold() in (article["title"] + " " + text).casefold()
                            for k in options["keywords"]
                        )
                        or options["authors"]
                        and not any(
                            a.casefold()
                            in (details["credit"] + " " + adapter["publisher"]).casefold()
                            for a in options["authors"]
                        )
                    ):
                        remember(url)
                        continue
                    grams = _grams(text)
                    if grams and any(
                        len(grams & _grams(old)) / len(grams) >= 0.40 for old in existing
                    ):
                        remember(url)
                        continue
                except AgentError as exc:
                    if exc.status >= 500:
                        raise
                    remember(url)
                    continue
                if rights_raw is None:
                    rights_raw = get(spec["rights_evidence"])
                    _rights(rights_raw, source_id)
                _proof(
                    core,
                    source_id,
                    article,
                    details,
                    page_raw,
                    catalog_raw,
                    rights_raw,
                    policy,
                    now,
                    len(ledger["records"]),
                )
                remember(url)
                return article
            next_page = progress["page"] if page == 1 and progress["page"] > 1 else following
            if next_page is None:
                progress.update(page=1, page_seen=[])
                _save_checkpoint(core, source_id, progress, checkpoint, now)
                return None
            if next_page != progress["page"]:
                progress.update(page=next_page, page_seen=[])
            _save_checkpoint(core, source_id, progress, checkpoint, now)
    return None
