"""Индивидуальный адаптер: EPFL. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_100",
    name="EPFL",
    host="actu.epfl.ch",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".ts-news-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("actu.epfl.ch",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
