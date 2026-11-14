"""Индивидуальный адаптер: NERC. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_087",
    name="NERC",
    host="www.ukri.org",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1.main-area__page-title",
    body_selector=".entry-content",
    date_selector=".post-summary__date time",
    credit_selector="figcaption",
    rights_reference="https://www.ukri.org/who-we-are/terms-of-use/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.ukri.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
