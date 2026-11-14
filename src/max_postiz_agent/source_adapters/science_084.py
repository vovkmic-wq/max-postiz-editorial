"""Индивидуальный адаптер: ESA Earth Online. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_084",
    name="ESA Earth Online",
    host="earth.esa.int",
    article_path="/eogateway/news/[a-z0-9-]+",
    title_selector="h2.text-brand-articleHeader",
    body_selector=".html-content",
    date_selector="",
    credit_selector="figcaption",
    rights_reference="https://www.esa.int/Services/Terms_and_conditions",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("earth.esa.int",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
