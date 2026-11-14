"""Индивидуальный адаптер: Chandra. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_041",
    name="Chandra",
    host="chandra.si.edu",
    article_path="/blog/node/[0-9]+",
    title_selector="h1.page-title",
    body_selector=".field--name-body",
    date_selector=".node-date time",
    credit_selector="figcaption",
    rights_reference="https://chandra.si.edu/photo/image_use.html",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
