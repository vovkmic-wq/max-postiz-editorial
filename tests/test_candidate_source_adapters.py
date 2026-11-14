"""Каждый кандидат проверяется отдельно и не получает runtime-допуск."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.source_adapters import REGISTRY
from max_postiz_agent.source_adapters.candidates import CANDIDATES

CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / "config/editorial_source_catalog.json").read_text(
        encoding="utf-8"
    )
)
ENTRIES = {x["id"]: x for x in CATALOG["sources"]}


@pytest.mark.parametrize("source_id", sorted(CANDIDATES))
def test_each_candidate_has_its_own_module_and_rejects_foreign_hosts(source_id):
    module = CANDIDATES[source_id]
    adapter = module.ADAPTER
    assert module.__name__.endswith("." + source_id)
    assert adapter.source_id == source_id
    assert source_id not in REGISTRY
    url = ENTRIES[source_id]["sample_url"]
    assert adapter.validate_url(url) == url
    if source_id == "science_077":
        with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
            adapter.validate_url("https://www.usgs.gov/news/featured-stories")
    for host in ("127.0.0.1", adapter.host + ".evil.example", "user@" + adapter.host):
        with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
            adapter.validate_url(url.replace(adapter.host, host, 1))


@pytest.mark.parametrize("source_id", sorted(CANDIDATES))
def test_each_candidate_rejects_oversize_and_ambiguous_html(source_id):
    adapter = CANDIDATES[source_id].ADAPTER
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_TOO_LARGE"):
        adapter._html(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_HTML_INVALID"):
        adapter._html(b'<a href="https://safe.example/" href="https://evil.example/">X</a>')


def test_candidate_preserves_rights_exception_and_all_paragraphs():
    adapter = replace(
        CANDIDATES["science_009"].ADAPTER,
        title_selector="h1",
        body_selector="article",
        date_selector="time",
        credit_selector=".credit",
    )
    raw = (
        '<h1>Engineering</h1><time datetime="2026-11-13"></time><article>'
        "<p>First complete scientific paragraph. " + "A measured result. " * 12 + "</p>"
        '<blockquote><p>A second paragraph with a <a href="/public/about/">link</a>.</p></blockquote>'
        '<p class="credit">This image is not released under our usual CC-BY license.</p>'
        "</article>"
    ).encode()
    result = adapter.extract(raw, "https://www.eso.org/public/blog/sarakrauss/")
    assert result["original"].count("A second paragraph") == 1
    assert result["links"][0]["url"] == "https://www.eso.org/public/about/"
    assert result["rights_notices"]
    assert "MATERIAL_RIGHTS_EXCEPTION_REQUIRES_REVIEW" in result["blockers"]
    assert not result["automatic_publication_enabled"]


def test_duplicate_exception_is_exact_and_never_applies_to_href():
    adapter = replace(
        CANDIDATES["science_002"].ADAPTER,
        duplicate_attributes=(("class", "img-responsive", "pr_news_feature_img img-responsive"),),
    )
    soup = adapter._html(b'<img class="img-responsive" class="pr_news_feature_img img-responsive">')
    assert soup.img["class"] == ["img-responsive"]
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_HTML_INVALID"):
        adapter._html(b'<img class="img-responsive" class="unreviewed">')
    unsafe = replace(adapter, duplicate_attributes=(("href", "one", "two"),))
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_HTML_INVALID"):
        unsafe._html(b'<a href="one" href="two">X</a>')


def test_heasarc_selects_latest_update_without_combining_old_news():
    text = "A complete instrument performance observation. " * 10
    raw = (
        '<div id="content"><div id="sec-wrapper">'
        '<h2 class="center">April 23, 2025: Performance update</h2><p>' + text + "</p>"
        '<h2 class="center">January 24, 2025: Old update</h2><p>Old unwanted data</p>'
        "</div></div>"
    ).encode()
    result = CANDIDATES["science_042"].extract(
        raw, "https://heasarc.gsfc.nasa.gov/docs/nicer/news/releases/patching_nicer_2025.html"
    )
    assert result["section_date"] == "2025-04-23"
    assert text.strip() in result["original"]
    assert "Old unwanted data" not in result["original"]
    assert "SECTION_IDENTITY_REVIEW_REQUIRED" in result["blockers"]
    assert result["section_html_sha256"] != result["html_sha256"]


def test_copernicus_migration_is_limited_to_observer_and_keeps_blockers():
    module = CANDIDATES["science_083"]
    adapter = module._adapter("https://eu-space.europa.eu/news/observer-science")
    assert adapter.validate_url("https://eu-space.europa.eu/news/observer-science")
    assert "SOURCE_CANONICAL_MIGRATION_REQUIRES_DEDUP_POLICY" in adapter.boundary_blockers
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
        adapter.validate_url("https://eu-space.europa.eu/news/unrelated-project")
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
        module._adapter("https://eu-space.europa.eu.evil.example/news/observer-science")
