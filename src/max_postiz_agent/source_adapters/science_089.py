"""Индивидуальный адаптер: Scripps Oceanography. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_089",
    name="Scripps Oceanography",
    host="scripps.ucsd.edu",
    article_path="/news/[a-z0-9-]+",
    title_selector="h1",
    body_selector=".field--name-body",
    date_selector=".jumbotron-news-article-date time",
    credit_selector=".header-credit",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(
        ("id", "block-globalnavigation", "block-globalnavigation"),
        ("role", "menuitem", "presentation"),
        ("id", "block-sio-ucsd-footer", "block-sio-ucsd-footer"),
    ),
    image_hosts=("scripps.ucsd.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
