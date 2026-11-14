"""Индивидуальный адаптер: SLAC. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_052",
    name="SLAC",
    host="www6.slac.stanford.edu",
    article_path="/news/20[0-9]{2}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+",
    title_selector="h1",
    body_selector="article",
    date_selector="time[datetime]",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www6.slac.stanford.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
