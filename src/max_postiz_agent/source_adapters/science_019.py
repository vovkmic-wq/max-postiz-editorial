"""Индивидуальный адаптер: Subaru Telescope. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_019",
    name="Subaru Telescope",
    host="subarutelescope.org",
    article_path="/en/results/20[0-9]{2}/[0-9]{2}/[0-9]{2}/[0-9]+\\.html",
    title_selector=".docs .titles h3",
    body_selector=".docs",
    date_selector="",
    credit_selector=".caption",
    rights_reference="https://subarutelescope.org/en/sitepolicy/",
    query_pattern="",
    exclusions=".linklist, .keywords, ul.share",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("subarutelescope.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
