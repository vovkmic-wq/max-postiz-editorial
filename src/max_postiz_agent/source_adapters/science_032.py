"""Индивидуальный адаптер: ASTRO 3D. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_032",
    name="ASTRO 3D",
    host="astro3d.org.au",
    article_path="/(?!category/|about/|contact/)[a-z0-9-]+/",
    title_selector="h1.entry-title",
    body_selector=".fusion-text-1",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=("DISCOVERY_SECTION_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("astro3d.org.au",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
