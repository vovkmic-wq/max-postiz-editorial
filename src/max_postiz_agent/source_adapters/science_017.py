"""Индивидуальный адаптер: SARAO. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_017",
    name="SARAO",
    host="www.sarao.ac.za",
    article_path="/news/[a-z0-9-]+/",
    title_selector="h1.post-title",
    body_selector=".entry-content",
    date_selector="time.date-container",
    credit_selector=".wp-caption-text",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(
        ("title", "Link to Facebook", "Link to Facebook"),
        ("title", "Link to X", "Link to X"),
        ("title", "Link to Instagram", "Link to Instagram"),
        ("title", "Link to LinkedIn", "Link to LinkedIn"),
        ("title", "Link to Youtube", "Link to Youtube"),
        ("title", "Link to TikTok", "Link to TikTok"),
        ("title", "Link to WhatsApp", "Link to WhatsApp"),
    ),
    image_hosts=("www.sarao.ac.za",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
