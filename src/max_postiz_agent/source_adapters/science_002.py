"""Индивидуальный адаптер: NOIRLab News. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_002",
    name="NOIRLab News",
    host="noirlab.edu",
    article_path="/public/news/noirlab[0-9]{4}/",
    title_selector="h1.pr_title",
    body_selector=".left-column",
    date_selector="p.date",
    credit_selector=".credit",
    rights_reference="https://noirlab.edu/public/copyright/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(
        ("class", "img-responsive", "pr_news_feature_img img-responsive"),
        ("class", "popup", "hidden"),
    ),
    image_hosts=("storage.noirlab.edu",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    return ADAPTER.extract(raw, article_url)
