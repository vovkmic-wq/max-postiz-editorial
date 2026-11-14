"""Отдельный адаптер раздела eso_potw."""

import re

SPEC = {
    "id": "eso_potw",
    "host": "www.eso.org",
    "path": "/public/images/potw[0-9]{4}[a-z]/",
    "rights_evidence": "https://www.eso.org/public/outreach/copyright/",
    "rights_text_sha256": "e796c958853697f99f5c571d09ca8386cc8e31f52aa2178743693f8156e5047e",
    "attribution": "CC BY 4.0",
}

CATALOG = {
    "catalog": "/public/images/potw/",
    "pages": "/public/images/potw/list/([1-9][0-9]{0,3})/",
    "page_template": "/public/images/potw/list/{page}/",
    "kind": "Picture of the Week",
    "publisher": "ESO",
    "rights_labels": {"Usage of ESO images, videos, web texts and music"},
    "newsletter_url": "https://www.eso.org/public/outreach/ai-disclaimer/",
    "newsletter_label": "This material is not AI generated",
}

IMAGE_HOST = ("cdn.eso.org", "/images/")


def primary_image_url(url):
    """Официальный архив ESO выбирается до запроса; это не fallback после ошибки."""
    match = re.fullmatch(
        r"https://cdn\.eso\.org/images/(wallpaper5|publicationjpg)/(potw[0-9]{4}[a-z]\.jpg)",
        url,
    )
    if match is None:
        from ..config import AgentError

        raise AgentError("ARTICLE_IMAGE_DOWNLOAD_REQUIRED", 409)
    return f"https://www.eso.org/public/archives/images/{match[1]}/{match[2]}"


TEXT_CREDIT = "ESO"

IMAGE_SECTION = "/public/images/"

ADAPTATION_NOTICE = "Перевод и адаптация с помощью ИИ."


def narrative_node(node):
    return node.name == "p"


def known_notice(anchor):
    # Только точное уведомление оригинала; наша подпись отдельно раскрывает ИИ.
    return str(anchor) == (
        '<a href="/public/outreach/ai-disclaimer/">This material is '
        "<strong>not</strong> AI generated</a>"
    )


def excluded_catalog_url(url):
    """Видео в общей ленте отклоняются до GET; их права не наследуются."""
    return re.fullmatch(r"https://www\.eso\.org/public/videos/potw[0-9]{4}[a-z]/", url) is not None
