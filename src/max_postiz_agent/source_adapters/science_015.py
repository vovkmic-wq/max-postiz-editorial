"""Индивидуальный адаптер: International Astronomical Union. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_015",
    name="International Astronomical Union",
    host="www.iau.org",
    article_path="/IAU/News/PR20[0-9]{2}/[A-Za-z0-9-]+\\.aspx",
    title_selector='div[id$="Panel_Title"] h1',
    body_selector='div[id$="Panel_odyText"]',
    date_selector="",
    credit_selector=".credit",
    rights_reference="",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.iau.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
