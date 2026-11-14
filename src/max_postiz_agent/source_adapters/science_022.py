"""Индивидуальный адаптер: Las Cumbres Observatory. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_022",
    name="Las Cumbres Observatory",
    host="lco.global",
    article_path="/news/[a-z0-9-]+/",
    title_selector="section.maincontent h2",
    body_selector="section.maincontent .column.is-three-quarters",
    date_selector="",
    credit_selector="figcaption",
    rights_reference="https://lco.global/observatory/acknowledgments/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("lco.global",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
