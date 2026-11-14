"""Индивидуальный адаптер: Fermilab. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_050",
    name="Fermilab",
    host="news.fnal.gov",
    article_path="/20[0-9]{2}/[0-9]{2}/[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".entry-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("news.fnal.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
