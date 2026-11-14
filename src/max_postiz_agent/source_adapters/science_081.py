"""Индивидуальный адаптер: EUMETSAT. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_081",
    name="EUMETSAT",
    host="www.eumetsat.int",
    article_path="/[a-z0-9-]+",
    title_selector="h1.regular",
    body_selector="article",
    date_selector=".publish-date",
    credit_selector="figcaption",
    rights_reference="https://www.eumetsat.int/about-us/terms-use",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED", "DISCOVERY_SECTION_REVIEW_REQUIRED"),
    duplicate_attributes=(("itemprop", "image", "image"),),
    image_hosts=("www-cdn.eumetsat.int",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
