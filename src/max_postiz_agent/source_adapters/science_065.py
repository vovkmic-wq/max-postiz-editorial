"""Индивидуальный адаптер: NASA Earth Observatory. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_065",
    name="NASA Earth Observatory",
    host="science.nasa.gov",
    article_path="/earth/earth-observatory/[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".entry-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".hds-credits",
    rights_reference="https://earthobservatory.nasa.gov/image-use-policy",
    query_pattern="",
    exclusions="",
    boundary_blockers=("DATED_ARTICLE_SEMANTICS_REQUIRED",),
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
