"""Индивидуальный адаптер: Astronomy Picture of the Day. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_068",
    name="Astronomy Picture of the Day",
    host="science.nasa.gov",
    article_path="/image-article/apod-20[0-9]{2}-[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".entry-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".hds-credits",
    rights_reference="https://apod.nasa.gov/apod/lib/about_apod.html",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(
        ("defer", "defer", "defer"),
        ("defer", "", "defer"),
        ("aria-current", "page", "true"),
    ),
    image_hosts=("assets.science.nasa.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
