"""Индивидуальный адаптер: Max Planck Institute for Astronomy. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_026",
    name="Max Planck Institute for Astronomy",
    host="www.mpia.de",
    article_path="/news/[a-z0-9-]+",
    title_selector="h1.serif",
    body_selector="article",
    date_selector="article .date",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="(?:c=[0-9]+)?",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.mpia.de",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
