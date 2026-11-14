"""Индивидуальный адаптер: National Solar Observatory. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_013",
    name="National Solar Observatory",
    host="nso.edu",
    article_path="/blog/[a-z0-9-]+/",
    title_selector="h1.entry-title",
    body_selector=".entry-content",
    date_selector='meta[property="article:published_time"]',
    credit_selector=".wp-caption-text",
    rights_reference="https://nso.edu/about/image-use-policy/",
    query_pattern="",
    exclusions=".et_bloom_form_container, .textwidget, .tnp-subscription",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("nso1.b-cdn.net",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
