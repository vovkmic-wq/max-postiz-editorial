"""Индивидуальный адаптер: ECMWF. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_082",
    name="ECMWF",
    host="www.ecmwf.int",
    article_path="/en/about/media-centre/news/20[0-9]{2}/[a-z0-9-]+",
    title_selector="h1.page-title",
    body_selector="article .field--name-body",
    date_selector=".field--name-field-publish-date time",
    credit_selector="figcaption",
    rights_reference="https://www.ecmwf.int/en/terms-use",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("www.ecmwf.int",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    result = ADAPTER.extract(raw, article_url)
    # ECMWF also publishes the hero image credit as a labelled paragraph.
    # Keep duplicates ambiguous and never infer a licence from this credit.
    soup = ADAPTER._html(raw)
    labels = []
    for node in soup.select("article .field--name-body p"):
        text = node.get_text(" ", strip=True)
        if text.startswith("Top image credit:"):
            labels.append(text.removeprefix("Top image credit:").strip())
    if labels:
        missing = "IMAGE_CREDIT_REQUIRED"
        result["credits"] = result["credits"] + labels
        if len(labels) == 1 and labels[0] and not soup.select(ADAPTER.credit_selector):
            if missing in result["blockers"]:
                result["blockers"].remove(missing)
        elif missing not in result["blockers"]:
            result["blockers"].append(missing)
    return result
