"""Индивидуальный адаптер: Carnegie Science. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_024",
    name="Carnegie Science",
    host="carnegiescience.edu",
    article_path="/news/[a-z0-9-]+",
    title_selector="h1.pageheader__title",
    body_selector="article",
    date_selector="article time",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions=".banner-cta, .media-gallery",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("carnegiescience.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
