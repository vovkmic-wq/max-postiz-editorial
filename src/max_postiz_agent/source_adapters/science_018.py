"""Индивидуальный адаптер: NAOJ. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_018",
    name="NAOJ",
    host="www.nao.ac.jp",
    article_path="/en/news/science/20[0-9]{2}/[a-z0-9-]+\\.html",
    title_selector="article h1",
    body_selector="article",
    date_selector="article time",
    credit_selector="figcaption",
    rights_reference="https://www.nao.ac.jp/en/terms/",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.nao.ac.jp",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
