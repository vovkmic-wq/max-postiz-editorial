"""Индивидуальный адаптер: HubbleSite. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_039",
    name="HubbleSite",
    host="science.nasa.gov",
    article_path="/mission/hubble/overview/[a-z0-9-]+/",
    title_selector="h1.page-heading-lg",
    body_selector=".entry-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".hds-credits",
    rights_reference="",
    query_pattern="",
    exclusions=".hds-content-item",
    boundary_blockers=("DATED_ARTICLE_SEMANTICS_REQUIRED",),
    duplicate_attributes=(
        ("defer", "defer", "defer"),
        ("defer", "", "defer"),
        ("aria-current", "page", "true"),
        ("id", "media-resources", "media-resources"),
        ("id", "social-media", "social-media"),
    ),
    image_hosts=("science.nasa.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
