"""Индивидуальный адаптер: ICRAR. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_030",
    name="ICRAR",
    host="www.icrar.org",
    article_path="/(?!category/|about/|contact/)[a-z0-9-]+/",
    title_selector="h1.h1",
    body_selector="article.article-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=("DISCOVERY_SECTION_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.icrar.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
