"""Индивидуальный адаптер: UK Space Agency. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_075",
    name="UK Space Agency",
    host="www.gov.uk",
    article_path="/government/news/[a-z0-9-]+",
    title_selector="h1.gem-c-heading__text",
    body_selector=".govspeak",
    date_selector="",
    credit_selector="figcaption",
    rights_reference="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.gov.uk",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
