"""Индивидуальный адаптер: WebbTelescope. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_040",
    name="WebbTelescope",
    host="science.nasa.gov",
    article_path="/blogs/webb/20[0-9]{2}/[0-9]{2}/[0-9]{2}/[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".single-blog-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".hds-credits",
    rights_reference="",
    query_pattern="",
    exclusions=".author-details, video",
    boundary_blockers=(),
    duplicate_attributes=(
        ("defer", "defer", "defer"),
        ("defer", "", "defer"),
        ("aria-current", "page", "true"),
        ("id", "Webb's-Blog", "Webb's-Blog"),
        ("id", "news", "news"),
        ("id", "Search-Filter-Webb-News", "Search-Filter-Webb-News"),
    ),
    image_hosts=("assets.science.nasa.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
