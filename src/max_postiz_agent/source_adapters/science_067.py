"""Индивидуальный адаптер: NASA Astrobiology. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_067",
    name="NASA Astrobiology",
    host="astrobiology.nasa.gov",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1",
    body_selector=".article-text",
    date_selector="",
    credit_selector="span.credit",
    rights_reference="https://www.nasa.gov/nasa-brand-center/images-and-media/",
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
