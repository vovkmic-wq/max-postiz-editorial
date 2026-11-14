"""Индивидуальный адаптер: CFHT. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_021",
    name="CFHT",
    host="www.cfht.hawaii.edu",
    article_path="/en/news/[A-Za-z0-9-]+/[A-Za-z0-9-]+\\.html",
    title_selector="article h1",
    body_selector="article",
    date_selector="",
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
