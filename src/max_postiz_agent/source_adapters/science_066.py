"""Индивидуальный адаптер: NASA Earthdata. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_066",
    name="NASA Earthdata",
    host="www.earthdata.nasa.gov",
    article_path="/news/worldview-image-archive/[a-z0-9-]+",
    title_selector="h1.field--name-title",
    body_selector="main",
    date_selector=".field--name-field-published-date time",
    credit_selector="figcaption",
    rights_reference="https://www.nasa.gov/nasa-brand-center/images-and-media/",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("earthdata.nasa.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
