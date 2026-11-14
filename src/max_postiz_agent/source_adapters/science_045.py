"""Индивидуальный адаптер: Virgo. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_045",
    name="Virgo",
    host="www.virgo-gw.eu",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1.display-4",
    body_selector="main",
    date_selector=".post-date",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
