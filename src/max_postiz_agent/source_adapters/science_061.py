"""Индивидуальный адаптер: NASA News. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_061",
    name="NASA News",
    host="www.nasa.gov",
    article_path="/blogs/spacestation/20[0-9]{2}/[0-9]{2}/[0-9]{2}/[a-z0-9-]+/",
    title_selector="h1",
    body_selector='[itemprop="articleBody"]',
    date_selector='meta[property="article:published_time"]',
    credit_selector=".hds-credits",
    rights_reference="https://www.nasa.gov/nasa-brand-center/images-and-media/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(
        ("defer", "defer", "defer"),
        ("defer", "", "defer"),
        ("aria-current", "page", "true"),
    ),
    image_hosts=("images-assets.nasa.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
