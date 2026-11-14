"""Индивидуальный адаптер: USGS. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_077",
    name="USGS",
    host="www.usgs.gov",
    article_path="/news/featured-story/[a-z0-9-]+",
    title_selector="h2.carousel-title",
    body_selector="main",
    date_selector="time[datetime]",
    credit_selector="figcaption",
    rights_reference="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(("aria-label", "Search", "Button"),),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
