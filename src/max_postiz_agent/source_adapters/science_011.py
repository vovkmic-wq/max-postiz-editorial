"""Индивидуальный адаптер: NRAO. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_011",
    name="NRAO",
    host="public.nrao.edu",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1.entry-title",
    body_selector=".nr-content-dc",
    date_selector="",
    credit_selector=".nrao-credit",
    rights_reference="https://public.nrao.edu/media-resources/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("public.nrao.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
