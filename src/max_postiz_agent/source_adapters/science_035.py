"""Индивидуальный адаптер: South African Astronomical Observatory. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_035",
    name="South African Astronomical Observatory",
    host="www.saao.ac.za",
    article_path="/20[0-9]{2}/[0-9]{2}/[0-9]{2}/[a-z0-9-]+/",
    title_selector=".post_text_inner h1",
    body_selector=".post_text_inner",
    date_selector="",
    credit_selector=".wp-caption-text",
    rights_reference="",
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
