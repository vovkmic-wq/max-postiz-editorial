"""Индивидуальный адаптер: Brookhaven National Laboratory. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_051",
    name="Brookhaven National Laboratory",
    host="www.bnl.gov",
    article_path="/newsroom/news\\.php",
    title_selector="h2#headline",
    body_selector=".articleBody",
    date_selector="",
    credit_selector=".caption",
    rights_reference="https://www.bnl.gov/newsroom/image-use.php",
    query_pattern="a=[0-9]+",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.bnl.gov",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
