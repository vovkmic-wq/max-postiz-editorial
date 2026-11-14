"""Индивидуальный адаптер: Natural History Museum London. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_093",
    name="Natural History Museum London",
    host="www.nhm.ac.uk",
    article_path="/discover/news/20[0-9]{2}/[a-z]+/[a-z0-9-]+\\.html",
    title_selector="h1.text-step-5",
    body_selector="main",
    date_selector="time",
    credit_selector="figcaption",
    rights_reference="https://www.nhm.ac.uk/business-services/licensing.html",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.nhm.ac.uk",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
