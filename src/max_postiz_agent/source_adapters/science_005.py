"""Индивидуальный адаптер: ESA Webb Picture of the Month. Допуск проверяется отдельно."""

from .candidate import CandidateAdapter

ADAPTER = CandidateAdapter(
    source_id="science_005",
    name="ESA Webb Picture of the Month",
    host="esawebb.org",
    article_path="/images/potm[0-9]{4}[a-z]/",
    title_selector="h1.my-3",
    body_selector=".left-column:has(> h1.my-3)",
    date_selector="",
    credit_selector=".credit",
    rights_reference="https://esawebb.org/copyright/",
    query_pattern="",
    exclusions="",
    boundary_blockers=(),
    duplicate_attributes=(),
    image_hosts=("cdn.esawebb.org",),
)


def discover(raw, index_url):
    return ADAPTER.discover(raw, index_url)


def extract(raw, article_url):
    result = ADAPTER.extract(raw, article_url)
    # Дата публикации находится в таблице метаданных, а не в повествовании.
    # Повтор строки считается неоднозначностью даже при одинаковом значении.
    soup = ADAPTER._html(raw)
    dates = []
    for row in soup.select("tr"):
        labels = row.find_all("th", recursive=False)
        cells = row.find_all("td", recursive=False)
        if len(labels) == 1 and labels[0].get_text(" ", strip=True) == "Release date:":
            dates.append(cells[0].get_text(" ", strip=True) if len(cells) == 1 else "")
    result["date_literals"] = dates
    if len(dates) == 1 and dates[0]:
        result["blockers"].remove("PUBLICATION_DATE_MISSING_OR_AMBIGUOUS")
    # Официальный JPEG выбирается только по ссылке текущей карточки.
    # Наличие ссылки не заменяет загрузку, проверку прав и native proof.
    ident = article_url.rstrip("/").rsplit("/", 1)[1]
    image_url = f"https://cdn.esawebb.org/archives/images/publicationjpg/{ident}.jpg"
    images = [
        image_url
        for anchor in soup.select("a[href]")
        if anchor.get("href") == image_url
        and anchor.get_text(" ", strip=True) == "Publication JPEG"
    ]
    result["image_candidates"] = sorted(set(images))
    missing = "SOURCE_IMAGE_CANDIDATE_REQUIRED"
    if images and missing in result["blockers"]:
        result["blockers"].remove(missing)
    elif not images and missing not in result["blockers"]:
        result["blockers"].append(missing)
    return result
