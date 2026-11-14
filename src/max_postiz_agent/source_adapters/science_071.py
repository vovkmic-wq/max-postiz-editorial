"""Индивидуальный адаптер: CNES. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_071",
    name="CNES",
    host="cnes.fr",
    article_path="/en/news/[a-z0-9-]+",
    title_selector="h1.cnes-event-hero-banner__title",
    body_selector="article.fr-evenement",
    date_selector=".cnes-event-hero-banner__date",
    credit_selector="figcaption",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(("type", "text", "search"),),
    image_hosts=("cnes.fr",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
