"""ESO is admitted by its exact section; legacy sources retain their footer."""

import pytest

from max_postiz_agent.article_attribution import article_footer
from max_postiz_agent.article_sources import validate_source_url
from max_postiz_agent.config import AgentError
from max_postiz_agent.server_science import _catalog


def test_eso_picture_section_has_exact_url_boundary():
    url = "https://www.eso.org/public/images/potw2645a/"
    assert validate_source_url("eso_potw", url) == url
    for invalid in (
        "https://www.eso.org/public/news/eso2645/",
        url + "?tracking=1",
        "https://www.eso.org.evil.example/public/images/potw2645a/",
    ):
        with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
            validate_source_url("eso_potw", invalid)


def test_eso_translation_discloses_ai_without_changing_hubble_footer():
    options = {"presentation_contract": "article-illustrated-v2", "image_credit": "ESO"}
    footer = article_footer("https://www.eso.org/public/images/potw2645a/", **options)
    assert "Перевод и адаптация с помощью ИИ." in footer
    assert "https://creativecommons.org/licenses/by/4.0/" in footer
    legacy = article_footer("https://esahubble.org/images/potw2645a/", **options)
    assert "Перевод и адаптация.\n" in legacy
    assert "с помощью ИИ" not in legacy


def test_eso_catalog_preserves_video_rejection_and_keeps_picture():
    cards = "".join(
        f'<a href="/public/{section}/potw2645a/">'
        '<div class="news-id">Picture of the Week</div>'
        '<div class="news-title">Stars</div>'
        '<div class="news-teaser"><strong>9 November 2026</strong></div></a>'
        for section in ("videos", "images")
    )
    raw = (
        '<html lang="en"><div class="news-list">' + cards + "</div>"
        '<ul class="pagination"><a href="/public/images/potw/list/2/">Next</a></ul></html>'
    ).encode()
    rejected = []
    items, following = _catalog(raw, "eso_potw", 1, rejected)
    assert [item["url"] for item in items] == ["https://www.eso.org/public/images/potw2645a/"]
    assert following == 2
    assert len(rejected) == 1
    assert rejected[0]["url"] == "https://www.eso.org/public/videos/potw2645a/"
    assert rejected[0]["classification"] == "unsupported_source_card"
    with pytest.raises(AgentError, match="SCIENCE_SOURCE_CATALOG_INVALID"):
        _catalog(
            raw.replace(b"/public/videos/potw2645a/", b"https://evil.example/video/"), "eso_potw", 1
        )
