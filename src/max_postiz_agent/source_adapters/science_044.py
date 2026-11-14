"""Индивидуальный адаптер: LIGO. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_044",
    name="LIGO",
    host="www.ligo.caltech.edu",
    article_path="/news/ligo[0-9]{8}",
    title_selector="h1",
    body_selector="div.content",
    date_selector="",
    credit_selector=".caption",
    rights_reference="https://www.ligo.caltech.edu/page/image-use-policy",
    query_pattern="",
    exclusions="",
    boundary_blockers=("ARTICLE_BOUNDARY_REVIEW_REQUIRED",),
    duplicate_attributes=(),
    image_hosts=("www.ligo.caltech.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
