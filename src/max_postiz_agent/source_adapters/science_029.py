"""Индивидуальный адаптер: Max Planck Society. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_029",
    name="Max Planck Society",
    host="www.mpg.de",
    article_path="/[0-9]+/[a-z0-9-]+",
    title_selector="h1.serif",
    body_selector="article",
    date_selector="article .date",
    credit_selector="figcaption",
    rights_reference="https://www.mpg.de/24758858/patents-licenses",
    query_pattern="(?:c=[0-9]+)?",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.mpg.de",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
