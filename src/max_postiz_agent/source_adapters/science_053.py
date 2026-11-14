"""Индивидуальный адаптер: Jefferson Lab. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_053",
    name="Jefferson Lab",
    host="www.jlab.org",
    article_path="/news/releases/[a-z0-9-]+",
    title_selector="h1",
    body_selector="article",
    date_selector="time[datetime]",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.jlab.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
