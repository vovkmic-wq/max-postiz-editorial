"""Индивидуальный адаптер: Canadian Space Agency. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_074",
    name="Canadian Space Agency",
    host="www.asc-csa.gc.ca",
    article_path="/eng/news/articles/20[0-9]{2}/20[0-9]{2}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+\\.asp",
    title_selector="h1#wb-cont",
    body_selector="main",
    date_selector="main time",
    credit_selector="figcaption",
    rights_reference="https://www.asc-csa.gc.ca/eng/terms.asp",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.asc-csa.gc.ca",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
