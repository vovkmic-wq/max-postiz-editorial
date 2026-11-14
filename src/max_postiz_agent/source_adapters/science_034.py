"""Индивидуальный адаптер: Swinburne. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_034",
    name="Swinburne",
    host="www.swinburne.edu.au",
    article_path="/news/20[0-9]{2}/[0-9]{2}/[a-z0-9-]+/?",
    title_selector="h1.h3",
    body_selector="article",
    date_selector="article time",
    credit_selector="figcaption",
    rights_reference="https://www.swinburne.edu.au/copyright-disclaimer/",
    query_pattern="",
    exclusions=".card",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=(),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
