"""Индивидуальный адаптер: Copernicus. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter
from dataclasses import replace
from urllib.parse import urlsplit

from ..config import AgentError

ADAPTER = CandidateAdapter(
    source_id="science_083",
    name="Copernicus",
    host="www.copernicus.eu",
    article_path="/en/news/news/[a-z0-9-]+",
    title_selector="h1.heading-underlined",
    body_selector="article.node--type-defis-eusp-news",
    date_selector="article time",
    credit_selector=".eusp-caption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("eu-space.europa.eu",),
)


def discover(raw, index_url):
    return _adapter(index_url).discover(raw, index_url)


def extract(raw, article_url):
    return _adapter(article_url).extract(raw, article_url)


def _adapter(url):
    """Проверенный перенос Observer в EU Space; права и дедуп требуют миграции."""
    host = urlsplit(url).netloc
    if host == ADAPTER.host:
        return ADAPTER
    if host != "eu-space.europa.eu":
        raise AgentError("ARTICLE_SOURCE_URL_INVALID", 409)
    return replace(
        ADAPTER,
        host=host,
        article_path=r"/news/observer-[a-z0-9-]+",
        boundary_blockers=ADAPTER.boundary_blockers
        + (
            "SOURCE_CANONICAL_MIGRATION_REQUIRES_DEDUP_POLICY",
            "RIGHTS_REFERENCE_MIGRATION_REQUIRED",
        ),
    )
