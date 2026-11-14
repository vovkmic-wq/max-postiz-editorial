"""Индивидуальный адаптер: Space Telescope Science Institute. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_038",
    name="Space Telescope Science Institute",
    host="www.stsci.edu",
    article_path="/contents/news-releases/20[0-9]{2}/news-20[0-9]{2}-[0-9]+",
    title_selector="h1#page-title",
    body_selector="main",
    date_selector=".news-release-date",
    credit_selector=".credit",
    rights_reference="",
    query_pattern="",
    exclusions=".modal, .subscribe",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("stsci-opo.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
