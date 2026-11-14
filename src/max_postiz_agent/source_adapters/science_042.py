"""Индивидуальный адаптер: HEASARC. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter
from dataclasses import replace
from datetime import datetime
import hashlib

from bs4 import Tag

from ..config import AgentError

ADAPTER = CandidateAdapter(
    source_id="science_042",
    name="HEASARC",
    host="heasarc.gsfc.nasa.gov",
    article_path="/docs/nicer/news/releases/[a-z0-9_]+\\.html",
    title_selector="h2.center",
    body_selector="#content",
    date_selector="",
    credit_selector=".credit",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(
        "ARTICLE_BOUNDARY_REVIEW_REQUIRED",
        "MULTIPLE_DATED_UPDATES_REQUIRE_ARTICLE_BOUNDARY",
    ),
    duplicate_attributes=(),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    """Выделяем самое новое датированное сообщение, не склеивая три обновления."""
    ADAPTER.validate_url(article_url)
    soup = ADAPTER._html(raw)
    headings = soup.select("#content h2.center")
    dated = []
    for heading in headings:
        literal = heading.get_text(" ", strip=True).split(":", 1)[0]
        try:
            stamp = datetime.strptime(literal, "%B %d, %Y")
        except ValueError as exc:
            raise AgentError("ARTICLE_SOURCE_DATE_INVALID", 409) from exc
        dated.append((stamp, heading))
    if not dated or len({stamp for stamp, _ in dated}) != len(dated):
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID", 409)
    stamp, heading = max(dated, key=lambda item: item[0])
    section = soup.new_tag("article")
    title = soup.new_tag("h1")
    title.string = heading.get_text(" ", strip=True)
    section.append(title)
    date = soup.new_tag("time", datetime=stamp.date().isoformat())
    section.append(date)
    for sibling in list(heading.next_siblings):
        if isinstance(sibling, Tag) and sibling.name == "h2":
            break
        section.append(sibling.extract())
    selected = replace(
        ADAPTER,
        title_selector="h1",
        body_selector="article",
        date_selector="time",
        boundary_blockers=("SECTION_IDENTITY_REVIEW_REQUIRED",),
    )
    result = selected.extract(str(section).encode("utf-8"), article_url)
    result["section_html_sha256"] = result["html_sha256"]
    result["html_sha256"] = hashlib.sha256(raw).hexdigest()
    result["section_date"] = stamp.date().isoformat()
    result["section_title"] = title.get_text(" ", strip=True)
    return result
