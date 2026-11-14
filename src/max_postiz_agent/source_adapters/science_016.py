"""Индивидуальный адаптер: SKAO. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_016",
    name="SKAO",
    host="www.skao.int",
    article_path="/en/news/[0-9]+/[a-z0-9-]+",
    title_selector="h1",
    body_selector="article.node--type-news",
    date_selector=".field--name-field-date time",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
