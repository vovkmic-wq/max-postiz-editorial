"""Индивидуальный адаптер: NASA Science. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_062",
    name="NASA Science",
    host="science.nasa.gov",
    article_path="/lunar-science/programs/[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".entry-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".hds-credits",
    rights_reference="https://www.nasa.gov/nasa-brand-center/images-and-media/",
    query_pattern="",
    exclusions="",
    boundary_blockers=("DATED_ARTICLE_SEMANTICS_REQUIRED",),
    duplicate_attributes=(
        ("defer", "defer", "defer"),
        ("defer", "", "defer"),
        ("aria-current", "page", "true"),
    ),
    image_hosts=("science.nasa.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
