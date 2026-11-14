"""Индивидуальный адаптер: ESA Webb News. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_006",
    name="ESA Webb News",
    host="esawebb.org",
    article_path="/news/weic[0-9]{4}/",
    title_selector="h1.pr_title",
    body_selector=".left-column",
    date_selector="p.date",
    credit_selector=".credit",
    rights_reference="https://esawebb.org/copyright/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("cdn.esawebb.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
