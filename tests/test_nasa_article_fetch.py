import importlib.util
from pathlib import Path

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.article_quality import normalize_article_links, rubric_translation_gate
from max_postiz_agent.collectors import clean_html

spec = importlib.util.spec_from_file_location("fetch_nasa_article", Path(__file__).parents[1] / "scripts/fetch_nasa_article.py")
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


def api(monkeypatch, status=200, **overrides):
    original = ("Earth's magnetosphere protects us from solar radiation. The Moon passes "
                "in and out of it as it orbits Earth. This visualization follows the Moon for 10 days.")
    payload = {"url": "http://svs.gsfc.nasa.gov/5648/", "page_type": "Visualization",
               "title": "The Moon and the magnetosphere", "description": "Search excerpt only.",
               "release_date": "2026-07-02T09:00:00-04:00",
               "media_groups": [{"widget": "Basic text", "description": original},
                                {"widget": "Video player", "description": "Unrelated caption"}]}
    payload.update(overrides)
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(status, json=payload)

    client_class = httpx.Client
    monkeypatch.setattr(fetcher.httpx, "Client", lambda **kw: client_class(**kw, transport=httpx.MockTransport(handle)))
    return calls, original


def test_real_metadata_http_reference_keeps_https_and_full_narrative(monkeypatch):
    calls, original = api(monkeypatch)
    result = fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert calls == ["https://svs.gsfc.nasa.gov/api/5648/"]
    assert result["original"] == original
    assert result["url"] == "https://svs.gsfc.nasa.gov/5648/"
    assert result["published_at"] > 0


@pytest.mark.parametrize("url", ["http://svs.gsfc.nasa.gov/5648/", "https://svs.gsfc.nasa.gov.evil.test/5648/",
                                  "https://svs.gsfc.nasa.gov/5648/?redirect=elsewhere"])
def test_source_url_rejected_before_request(monkeypatch, url):
    calls, _ = api(monkeypatch)
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_NOT_ALLOWED"):
        fetcher.fetch_article(url)
    assert calls == []


def test_access_denial_stops_without_retry(monkeypatch):
    calls, _ = api(monkeypatch, status=403)
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_UNAVAILABLE"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert len(calls) == 1


def test_no_fallback_to_search_excerpt(monkeypatch):
    api(monkeypatch, media_groups=[])
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_NARRATIVE_REQUIRED"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")


def test_documented_html_narrative_is_cleaned_without_media_caption(monkeypatch):
    text = ("Earth's gravity measurements help scientists follow water across the planet. "
            "This scientific description is the complete narrative, not a video caption.")
    calls, _ = api(monkeypatch, media_groups=[
        {"widget": "Basic text with HTML", "description": "<p>" + text + "</p>"},
        {"widget": "Video player", "description": "Caption must never replace the narrative."},
    ])
    result = fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert calls == ["https://svs.gsfc.nasa.gov/api/5648/"]
    assert result["original"] == text


def test_media_descriptions_alone_are_not_an_article(monkeypatch):
    api(monkeypatch, media_groups=[
        {"widget": "Video player", "description": "A long caption masquerading as an article. " * 10},
    ])
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_NARRATIVE_REQUIRED"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")


@pytest.mark.parametrize("widget", ["Basic text", "Basic text with HTML"])
def test_copyright_exception_requires_review(monkeypatch, widget):
    api(monkeypatch, media_groups=[{"widget": widget, "description": "All rights reserved. " * 12}])
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")


DOI = "https://doi.org/10.3847/1538-3881/ad6b23"
PAPER_LABEL = "2024 paper published in the Astronomical Journal"
ECLIPSE = (
    "Traditional solar eclipse maps depict the shape of the Moon's umbra shadow as a smooth oval, "
    "but more detailed calculations that account for the Moon's terrain have revealed that the shape is a many-sided polygon. "
    f'A <a href="{DOI}">2024 paper published in <em>the Astronomical Journal</em></a> '
    "explains how the mountains and valleys around the limb (silhouette edge) of the Moon create this complex shape."
    "<br><br>During a total solar eclipse, the valleys act like pinholes projecting images of the Sun onto the Earth's surface."
)


def test_nasa_5366_doi_and_every_visible_word_are_retained_without_extra_requests(monkeypatch):
    calls, _ = api(monkeypatch, url="http://svs.gsfc.nasa.gov/5366/", media_groups=[
        {"id": 375441, "widget": "Basic text", "description": ECLIPSE},
        {"widget": "Video player", "description": '<a href="https://media.example.org/movie.mp4">Not narrative</a>'},
    ])
    result = fetcher.fetch_article("https://svs.gsfc.nasa.gov/5366/")
    link = f"[{PAPER_LABEL}]({DOI})"
    assert link in result["original"]
    assert result["original"].replace(link, PAPER_LABEL) == clean_html(ECLIPSE)
    assert "movie.mp4" not in result["original"] and "Not narrative" not in result["original"]
    assert calls == ["https://svs.gsfc.nasa.gov/api/5366/"]


def test_fetched_doi_markdown_is_compatible_with_existing_article_links_and_numeric_gate(monkeypatch):
    api(monkeypatch, media_groups=[{"widget": "Basic text with HTML", "description": ECLIPSE}])
    original = fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")["original"]
    translated = ("Подробные расчёты учитывают рельеф Луны и показывают многоугольную форму её тени. "
        f"В [статье 2024 года в астрономическом журнале]({DOI}) описано, как горы и долины на краю Луны "
        "создают эту сложную форму. Во время полного солнечного затмения долины проецируют изображения "
        "Солнца на поверхность Земли.")
    normalized = normalize_article_links(translated)
    assert normalized.endswith("\n\n" + DOI) and "[статье" not in normalized
    rubric_translation_gate(original, normalized, "meaning-first-v1")


@pytest.mark.parametrize("href,expected", [
    ("https://doi.org/paper", "https://doi.org/paper"),
    ("http://example.org/research", "http://example.org/research"),
    ("../5444/", "https://svs.gsfc.nasa.gov/5444/"),
    ("/vis/a000000/a005000/data.txt", "https://svs.gsfc.nasa.gov/vis/a000000/a005000/data.txt"),
    ("#media_group_375441", "https://svs.gsfc.nasa.gov/5648/#media_group_375441"),
    ("//science.nasa.gov/research/", "https://science.nasa.gov/research/"),
    ("https://example.org/paper_(2024)", "https://example.org/paper_(2024)"),
    ("https://example.org/research%20paper?q=one&amp;source=NASA", "https://example.org/research%20paper?q=one&source=NASA"),
])
def test_absolute_and_relative_links_use_canonical_source_base_without_fetching_targets(monkeypatch, href, expected):
    text = ("Scientists compared their complete observations before publishing the scientific results. "
            f'Read <a href="{href}"><strong>the complete research</strong></a> for the evidence and methods.')
    calls, _ = api(monkeypatch, media_groups=[{"widget": "Basic text with HTML", "description": text}])
    result = fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648")
    assert f"[the complete research]({expected})" in result["original"]
    assert calls == ["https://svs.gsfc.nasa.gov/api/5648/"]


@pytest.mark.parametrize("href", ["", " ", "javascript:alert(1)", "JaVaScRiPt:alert(1)",
    "java&#x09;script:alert(1)", "&#106;avascript:alert(1)", "%6Aavascript%3Aalert(1)",
    "%256Aavascript%253Aalert(1)", "data:text/html,unsafe", "mailto:test@example.org", "ftp://example.org/paper",
    "https://reader:password@example.org/paper", "//reader@example.org/paper", "https://reader%40example.org/paper",
    "https://example.org/paper%0aunsafe", "https://example.org/unsafe%5Cpath", "https://example.org/%GG",
    "https://example.org:invalid/paper", "https:///", "https://example.org/paper)",
])
def test_unsafe_empty_or_obfuscated_links_fail_explicitly_without_extra_requests(monkeypatch, href):
    calls, _ = api(monkeypatch, media_groups=[{"widget": "Basic text", "description": (
        "Scientists studied the complete astronomical observations and documented every result. "
        f'The <a href="{href}">complete source paper</a> gives all the scientific methods.') }])
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_INVALID"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert calls == ["https://svs.gsfc.nasa.gov/api/5648/"]


@pytest.mark.parametrize("anchor", [
    '<a href="https://doi.org/safe" href="javascript:unsafe">paper</a>',
    '<a>paper</a>', '<a href="https://doi.org/empty"></a>',
    '<a href="https://doi.org/paper">[paper]</a>',
    '<a href="https://doi.org/outer"><a href="https://doi.org/inner">paper</a></a>',
])
def test_ambiguous_or_unrenderable_anchors_fail_closed(monkeypatch, anchor):
    calls, _ = api(monkeypatch, media_groups=[{"widget": "Basic text", "description": (
        "Researchers reviewed all the scientific evidence before publishing their findings. "
        + anchor + " provides the complete analysis and supporting observations.") }])
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_(INVALID|LABEL_INVALID)"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert len(calls) == 1


@pytest.mark.parametrize("narrative", [
    'Researchers describe <a href="https://doi.org/paper">their paper. The remaining narrative must not become its label.',
    "Researchers describe their paper.</a> The remaining narrative has an unmatched closing anchor and must be rejected.",
])
def test_unbalanced_anchors_do_not_silently_capture_or_discard_narrative(monkeypatch, narrative):
    calls, _ = api(monkeypatch, media_groups=[{"widget": "Basic text with HTML", "description": narrative}])
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_INVALID"):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert calls == ["https://svs.gsfc.nasa.gov/api/5648/"]


def test_multiple_links_and_plain_narrative_keep_original_order_and_words(monkeypatch):
    first = ('The researchers describe <a href="../5444/">their methods</a>, then compare '
             '<a href="https://doi.org/example">the complete results</a> before publishing the observations.')
    second = "A plain narrative remains unchanged and gives the full scientific context of this comparison."
    calls, _ = api(monkeypatch, media_groups=[{"widget": "Basic text", "description": first},
                                             {"widget": "Basic text with HTML", "description": "<p>" + second + "</p>"}])
    result = fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert result["original"] == ("The researchers describe [their methods](https://svs.gsfc.nasa.gov/5444/), then compare "
        "[the complete results](https://doi.org/example) before publishing the observations.\n\n" + second)
    assert len(calls) == 1


@pytest.mark.parametrize("change,code", [({"page_type": "Story"}, "ARTICLE_SOURCE_METADATA_INVALID"),
    ({"url": "https://svs.gsfc.nasa.gov/9999/"}, "ARTICLE_SOURCE_METADATA_INVALID"),
    ({"release_date": "2026-07-02T09:00:00"}, "ARTICLE_SOURCE_DATE_INVALID"),
    ({"media_groups": [{"widget": "Basic text", "description": "x" * 7501}]}, "ARTICLE_SOURCE_LENGTH_INVALID")])
def test_metadata_date_and_size_guards_still_apply(monkeypatch, change, code):
    calls, _ = api(monkeypatch, **change)
    with pytest.raises(AgentError, match=code):
        fetcher.fetch_article("https://svs.gsfc.nasa.gov/5648/")
    assert len(calls) == 1
