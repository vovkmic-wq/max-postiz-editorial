"""Индивидуальный адаптер: ALMA. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_010",
    name="ALMA",
    host="www.almaobservatory.org",
    article_path="/en/press-releases/[a-z0-9-]+/",
    title_selector="article h1",
    body_selector=".article-reading-content",
    date_selector=".dates",
    credit_selector="figcaption",
    rights_reference="https://www.almaobservatory.org/en/copyright-notice/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.almaobservatory.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
