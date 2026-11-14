"""Индивидуальный адаптер: Leibniz AIP. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_025",
    name="Leibniz AIP",
    host="www.aip.de",
    article_path="/en/news/[a-z0-9-]+/",
    title_selector="h1.headline__text",
    body_selector="main",
    date_selector="",
    credit_selector=".img-caption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(
        ("class", "footer__sitemap-link footer__sitemap-link--level-1", "no-hyphens"),
    ),
    image_hosts=("www.aip.de",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
