"""Индивидуальный адаптер: Einstein Online. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_047",
    name="Einstein Online",
    host="www.einstein-online.info",
    article_path="/en/spotlight/[A-Za-z0-9-]+/",
    title_selector="h1.single_title",
    body_selector="#content",
    date_selector="",
    credit_selector=".wp-caption-text",
    rights_reference="https://www.einstein-online.info/en/about-einstein-online/copyright-policy/",
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
