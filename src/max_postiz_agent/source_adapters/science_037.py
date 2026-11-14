"""Индивидуальный адаптер: Harvard Smithsonian CfA. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_037",
    name="Harvard Smithsonian CfA",
    host="www.cfa.harvard.edu",
    article_path="/news/[a-z0-9-]+",
    title_selector="h1",
    body_selector="#main-content",
    date_selector="",
    credit_selector=".caption-description",
    rights_reference="https://www.cfa.harvard.edu/news/image-use-policy",
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
