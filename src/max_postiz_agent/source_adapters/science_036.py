"""Индивидуальный адаптер: DIAS. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_036",
    name="DIAS",
    host="www.dias.ie",
    article_path="/20[0-9]{2}/[0-9]{2}/[0-9]{2}/[a-z0-9-]+/",
    title_selector=".post-banner h1",
    body_selector="div.article",
    date_selector="",
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(("rel", "me", "nofollow noopener noreferrer"), ("rel", "me", "nofollow")),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
