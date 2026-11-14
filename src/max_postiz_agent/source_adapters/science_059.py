"""Индивидуальный адаптер: Lawrence Livermore. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_059",
    name="Lawrence Livermore",
    host="www.llnl.gov",
    article_path="/article/[0-9]+/[a-z0-9-]+",
    title_selector="h1",
    body_selector=".field--name-body",
    date_selector="time[datetime]",
    credit_selector="figcaption",
    rights_reference="https://www.llnl.gov/copyright-reuse",
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
