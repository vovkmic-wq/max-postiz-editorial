"""Индивидуальный адаптер: ESO Blog. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_009",
    name="ESO Blog",
    host="www.eso.org",
    article_path="/public/blog/[a-z0-9-]+/",
    title_selector=".post .header .title h1",
    body_selector=".col-md-12.body",
    date_selector=".date",
    credit_selector=".credit",
    rights_reference="https://www.eso.org/public/outreach/copyright/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("cdn.eso.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
