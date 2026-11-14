"""Индивидуальный адаптер: CSIRO. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_031",
    name="CSIRO",
    host="www.csiro.au",
    article_path="/en/news/All/Articles/20[0-9]{2}/[A-Za-z]+/[A-Za-z0-9-]+",
    title_selector="h1",
    body_selector="article",
    date_selector="",
    credit_selector="figcaption",
    rights_reference="https://www.csiro.au/en/about/Policies/Legal/Copyright",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(("id", "twitter", "svg-a205q1c87"),),
    image_hosts=("www.csiro.au",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
