"""Индивидуальный адаптер: MBARI. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_090",
    name="MBARI",
    host="www.mbari.org",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1.masthead__title",
    body_selector="article.single-post",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions=".related-content, .visc-social-share",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.mbari.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
