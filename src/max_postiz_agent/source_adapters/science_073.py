"""Индивидуальный адаптер: ISRO. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_073",
    name="ISRO",
    host="www.isro.gov.in",
    article_path="/updates_[A-Za-z0-9_-]+\\.html",
    title_selector="h1.pageHeading",
    body_selector="main",
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
