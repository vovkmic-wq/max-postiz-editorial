"""Индивидуальный адаптер: AURA. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_014",
    name="AURA",
    host="www.aura-astronomy.org",
    article_path="/blog/20[0-9]{2}/[0-9]{2}/[0-9]{2}/[a-z0-9-]+/",
    title_selector="h1.post-title",
    body_selector=".post-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.aura-astronomy.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
