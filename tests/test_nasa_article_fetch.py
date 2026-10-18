import importlib.util
from pathlib import Path

import httpx
import pytest

from max_postiz_agent.config import AgentError

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
