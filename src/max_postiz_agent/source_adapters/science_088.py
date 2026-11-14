"""Индивидуальный адаптер: Woods Hole Oceanographic Institution. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_088",
    name="Woods Hole Oceanographic Institution",
    host="www.whoi.edu",
    article_path="/press-room/news-release/[a-z0-9-]+/",
    title_selector="h1.fl-post-title",
    body_selector=".fl-post-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.whoi.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
