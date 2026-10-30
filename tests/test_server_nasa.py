"""Полные NASA-тексты из ограниченных JSON; медиа и модели не вызываются."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Cursor
from max_postiz_agent.reddit import text_hash
from max_postiz_agent.server_nasa import select_nasa_article

pytest_plugins = ["test_article_cycle"]

STAMP = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
URL = "https://svs.gsfc.nasa.gov/1234/"
TEXT = ("Researchers observe the planet and explain how satellite measurements improve our understanding "
        "of the changing atmosphere. Every observation is compared with other measurements. ")


@pytest.fixture
def source(tmp_path, monkeypatch):
    rights = {"verified": True, "allow_storage": True, "allow_derivative": True,
              "evidence_ref": "https://svs.gsfc.nasa.gov/help/"}
    settings = SimpleNamespace(root=tmp_path, source=lambda _: {"enabled": True, "rights": rights})
    core = SimpleNamespace(settings=settings)
    monkeypatch.setattr("max_postiz_agent.server_nasa._used_narratives", lambda *_: [])
    options = {"authors": [], "keywords": [], "min_rating": 0, "max_candidates": 3, "max_words": 500}
    policy = {"source_id": "nasa_svs_articles", "allowed_hosts": ["svs.gsfc.nasa.gov"],
              "rights_evidence": rights["evidence_ref"], "max_source_age_hours": 8760}
    page = {"id": 1234, "url": URL, "page_type": "Visualization", "title": "Atmospheric measurements",
            "release_date": "2026-10-01T12:00:00-04:00",
            "main_credits": {"Visualizers": [{"name": "Jane Scientist", "employer": "NASA/GSFC"}]},
            "media_groups": [{"id": 12, "widget": "Basic text with HTML", "description": TEXT},
                             {"widget": "Video player", "description": "This caption must not enter the article.",
                              "items": [{"url": "https://media.invalid/file.mp4"}]}]}
    requests = []

    def handler(request):
        requests.append(str(request.url))
        assert request.method == "GET"
        if request.url.path == "/api/search/":
            return httpx.Response(200, json={"results": [{k: page[k] for k in ("id", "page_type", "release_date")} ]})
        assert request.url.path == "/api/1234/"
        return httpx.Response(200, json=page)

    def run(ledger=None, transport=None):
        return select_nasa_article(core, options, ledger or {"records": []}, policy,
                                   now=STAMP, transport=transport or httpx.MockTransport(handler))
    return SimpleNamespace(core=core, options=options, policy=policy, page=page, requests=requests,
                           run=run, rights=rights, root=tmp_path)


def test_full_basic_link_and_all_sections_preserved_without_media(source):
    source.page["media_groups"][0]["description"] = TEXT + '<a href="https://science.nasa.gov/data/">Full dataset</a>'
    source.page["media_groups"].append({"widget": "Basic text", "description": "Second complete scientific paragraph about the atmosphere."})
    article = source.run()
    assert article["original"] == TEXT.strip() + ' [Full dataset](https://science.nasa.gov/data/)\n\nSecond complete scientific paragraph about the atmosphere.'
    assert "caption must not" not in article["original"]
    assert len(source.requests) == 2
    proofs = list((source.root/"data/server-sources").glob("*.local.json"))
    assert len(proofs) == 1
    saved = json.loads(proofs[0].read_text(encoding="utf-8"))
    assert saved["page"] == source.page and saved["article"] == article


@pytest.mark.parametrize("change", ["animation", "missing_basic", "rights_exception", "missing_credits", "bad_href", "stale", "future"])
def test_invalid_or_unpermitted_whole_source_is_skipped(source, change):
    if change == "animation":
        source.page["page_type"] = "Animation"
    if change == "missing_basic":
        source.page["media_groups"] = []
    if change == "rights_exception":
        source.page["media_groups"][0]["description"] += " Copyright someone."
    if change == "missing_credits":
        source.page["main_credits"] = {}
    if change == "bad_href":
        source.page["media_groups"][0]["description"] += '<a href="javascript:bad()">bad</a>'
    if change == "stale":
        source.page["release_date"] = "2024-01-01T00:00:00+00:00"
    if change == "future":
        source.page["release_date"] = "2027-01-01T00:00:00+00:00"
    assert source.run() is None
    assert not (source.root/"data/server-sources").exists()


def test_duplicate_url_including_cancelled_is_skipped_before_full_get(source):
    assert source.run({"records": [{"url": URL, "state": "cancelled"}]}) is None
    assert len(source.requests) == 1


def test_duplicate_normalized_source_and_boilerplate_are_skipped(source, monkeypatch):
    assert source.run({"records": [{"url": "https://svs.gsfc.nasa.gov/old/", "original_hash": text_hash(TEXT)}]}) is None
    monkeypatch.setattr("max_postiz_agent.server_nasa._used_narratives", lambda *_: [TEXT])
    assert source.run() is None


def test_authors_keywords_filter_the_full_official_page(source):
    source.options.update(authors=["Jane Scientist"], keywords=["changing atmosphere"])
    assert source.run() is not None
    source.options["authors"] = ["Other Person"]
    assert source.run() is None


def test_rating_without_verified_nasa_scale_fails_before_http(source):
    source.options["min_rating"] = 5
    with pytest.raises(AgentError, match="NASA_RATING_UNAVAILABLE"):
        source.run()
    assert source.requests == []


@pytest.mark.parametrize("failure", ["timeout", "json", "array"])
def test_known_get_failure_is_normalized_without_another_request(source, failure):
    requests = []
    def handler(request):
        requests.append(str(request.url))
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream timed out", request=request)
        if failure == "json":
            return httpx.Response(200, content=b"not JSON")
        return httpx.Response(200, json=[])
    code = "NASA_SOURCE_UNAVAILABLE" if failure == "timeout" else "NASA_SOURCE_CATALOG_INVALID"
    with pytest.raises(AgentError, match=code):
        source.run(transport=httpx.MockTransport(handler))
    assert len(requests) == 1


def test_unverified_rights_fail_before_http(source):
    source.rights["verified"] = False
    with pytest.raises(AgentError, match="ARTICLE_RIGHTS_REQUIRED"):
        source.run()
    assert source.requests == []


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_infrastructure_and_redirect_never_trigger_retry_or_media(source, status):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://elsewhere.invalid/"})
    with pytest.raises(AgentError, match="NASA_SOURCE_UNAVAILABLE"):
        source.run(transport=httpx.MockTransport(handler))
    assert len(seen) == 1


def test_catalog_size_bound_is_fail_closed(source):
    def handler(_request):
        return httpx.Response(200, json={"results": [copy.deepcopy(source.page)] * 4})
    with pytest.raises(AgentError, match="NASA_SOURCE_CATALOG_INVALID"):
        source.run(transport=httpx.MockTransport(handler))


def test_private_source_exclusions_are_respected_before_full_get(source):
    directory = source.root / "data"
    directory.mkdir()
    (directory / "nasa-source-exclusions.local.json").write_text(json.dumps({
        "excluded": [{"url": URL, "reason": "Verified conflicting year; no silent repair"}]}), encoding="utf-8")
    assert source.run() is None
    assert len(source.requests) == 1


def test_invalid_exclusions_fail_before_any_http(source):
    directory = source.root / "data"
    directory.mkdir()
    (directory / "nasa-source-exclusions.local.json").write_text('{"excluded": "bad"}', encoding="utf-8")
    with pytest.raises(AgentError, match="NASA_EXCLUSIONS_INVALID"):
        source.run()
    assert source.requests == []


@pytest.mark.parametrize("page_type, accepted", [("Visualization", True), ("Animation", False)])
def test_real_search_without_page_type_requires_strict_full_page_type(source, page_type, accepted):
    source.page["page_type"] = page_type
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/search/":
            # Реальный Search API содержит id/title/release_date, но не page_type.
            return httpx.Response(200, json={"results": [{"id": 1234, "title": source.page["title"],
                "release_date": source.page["release_date"]}]})
        assert request.url.path == "/api/1234/"
        return httpx.Response(200, json=source.page)

    result = source.run(transport=httpx.MockTransport(handler))
    assert (result is not None) is accepted
    assert seen == ["/api/search/", "/api/1234/"]


def test_bounded_pagination_and_restart_checkpoint_avoid_head_budget_livelock(source, cycle):
    source.core.db = cycle[1].db
    source.options["max_candidates"] = 3
    seen = []
    head_ids = [1, 2, 3]

    def handler(request):
        if request.url.path == "/api/search/":
            offset = int(request.url.params.get("offset", 0))
            seen.append(("catalog", offset))
            ids = head_ids if offset == 0 else [4]
            return httpx.Response(200, json={"results": [{"id": i, "release_date": source.page["release_date"]} for i in ids],
                "next": "http://svs.gsfc.nasa.gov/api/search/?studio=SVS&limit=3&offset=3" if offset == 0 else None})
        ident = int(request.url.path.strip("/").split("/")[-1])
        seen.append(("full", ident))
        page = copy.deepcopy(source.page)
        page.update(id=ident, url=f"https://svs.gsfc.nasa.gov/{ident}/")
        if ident in {1, 2, 3}:
            page["page_type"] = "Animation"
        return httpx.Response(200, json=page)

    transport = httpx.MockTransport(handler)
    def run():
        return select_nasa_article(source.core, source.options, {"records": []}, source.policy,
            now=STAMP, transport=transport, checkpoint=True)
    assert run() is None
    assert sum(k == "full" for k, _ in seen) == 3
    assert sum(k == "catalog" for k, _ in seen) <= 3
    with source.core.db.session() as session:
        data = session.get(Cursor, "server_nasa_svs").data
        assert data["offset"] == 3 and set(data["head_seen"]) == {1, 2, 3}
    seen.clear()
    assert run()["url"] == "https://svs.gsfc.nasa.gov/4/"
    assert seen == [("catalog", 0), ("catalog", 3), ("full", 4)]
    seen.clear()
    head_ids[:] = [99, 1, 2]
    assert run()["url"] == "https://svs.gsfc.nasa.gov/99/"
    assert seen == [("catalog", 0), ("full", 99)]


def test_catalog_pages_are_bounded_and_end_resets_checkpoint(source, cycle):
    source.core.db = cycle[1].db
    source.options["max_candidates"] = 3
    seen = []

    def handler(request):
        assert request.url.path == "/api/search/"
        offset = int(request.url.params.get("offset", 0))
        seen.append(offset)
        return httpx.Response(200, json={"results": [], "next": (
            f"http://svs.gsfc.nasa.gov/api/search/?studio=SVS&limit=3&offset={offset+3}" if offset < 9 else None)})

    def run():
        return select_nasa_article(source.core, source.options, {"records": []}, source.policy,
            now=STAMP, transport=httpx.MockTransport(handler), checkpoint=True)
    assert run() is None and seen == [0, 3, 6]
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs").data["offset"] == 9
    seen.clear()
    assert run() is None and seen == [0, 9]
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs").data["offset"] == 0


@pytest.mark.parametrize("next_url", ["https://evil.invalid/api/search/?offset=3", "https://svs.gsfc.nasa.gov/api/search/?studio=SVS&limit=3&offset=0"])
def test_untrusted_or_nonadvancing_next_never_becomes_a_request(source, next_url):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": [], "next": next_url})
    with pytest.raises(AgentError, match="NASA_SOURCE_CATALOG_INVALID"):
        source.run(transport=httpx.MockTransport(handler))
    assert len(seen) == 1


def test_readonly_default_pagination_does_not_create_a_checkpoint(source, cycle):
    source.core.db = cycle[1].db
    assert source.run() is not None
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs") is None


def test_checkpoint_keeps_only_known_skips_before_a_transport_failure(source, cycle):
    source.core.db = cycle[1].db

    def handler(request):
        if request.url.path == "/api/search/":
            return httpx.Response(200, json={"results": [
                {"id": i, "release_date": source.page["release_date"]} for i in [1, 2]], "next": None})
        if request.url.path == "/api/2/":
            return httpx.Response(503)
        page = copy.deepcopy(source.page)
        page.update(id=1, url="https://svs.gsfc.nasa.gov/1/", page_type="Animation")
        return httpx.Response(200, json=page)

    with pytest.raises(AgentError, match="NASA_SOURCE_UNAVAILABLE"):
        select_nasa_article(source.core, source.options, {"records": []}, source.policy,
            now=STAMP, transport=httpx.MockTransport(handler), checkpoint=True)
    with source.core.db.session() as session:
        data = session.get(Cursor, "server_nasa_svs").data
        assert data["head_seen"] == [1] and data["offset"] == 0


@pytest.mark.parametrize("future_date", ["catalog", "page"])
def test_future_release_is_rechecked_at_due_time_without_remembering(source, cycle, future_date):
    source.core.db = cycle[1].db
    original_date = source.page["release_date"]
    source.page["release_date"] = "2026-10-22T01:00:00+00:00"
    due = STAMP + 3600
    seen = []

    def handler(request):
        assert request.method == "GET"
        seen.append(request.url.path)
        if request.url.path == "/api/search/":
            released = source.page["release_date"] if future_date == "catalog" else original_date
            return httpx.Response(200, json={"results": [{"id": 1234, "release_date": released}]})
        assert request.url.path == "/api/1234/"
        return httpx.Response(200, json=source.page)

    def run(stamp):
        return select_nasa_article(source.core, source.options, {"records": []}, source.policy,
            now=stamp, transport=httpx.MockTransport(handler), checkpoint=True)

    expected = ["/api/search/"] + (["/api/1234/"] if future_date == "page" else [])
    for stamp in (STAMP, due - 1):
        seen.clear()
        assert run(stamp) is None
        assert seen == expected
        with source.core.db.session() as session:
            assert session.get(Cursor, "server_nasa_svs").data["head_seen"] == []
        assert not (source.root / "data/server-sources").exists()

    seen.clear()
    article = run(due)
    assert article["url"] == URL and article["published_at"] == due
    assert seen == ["/api/search/", "/api/1234/"]
    assert len(list((source.root / "data/server-sources").glob("*.local.json"))) == 1
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs").data["head_seen"] == [1234]
    seen.clear()
    assert run(due) is None and seen == ["/api/search/"]
    assert not cycle[3] and not cycle[4]


@pytest.mark.parametrize("other_error", [None, "rights", "href", "type", "credits", "stale"])
def test_erroneous_future_page_date_is_rechecked_without_bypassing_other_rejections(
    source, cycle, other_error
):
    source.core.db = cycle[1].db
    catalog_date = source.page["release_date"]
    source.page["release_date"] = "2027-01-01T00:00:00+00:00"
    if other_error == "rights":
        source.page["media_groups"][0]["description"] += " Copyright someone."
    elif other_error == "href":
        source.page["media_groups"][0]["description"] += '<a href="javascript:bad()">bad</a>'
    elif other_error == "type":
        source.page["page_type"] = "Animation"
    elif other_error == "credits":
        source.page["main_credits"] = {}
    seen = []

    def handler(request):
        assert request.method == "GET"
        seen.append(request.url.path)
        if request.url.path == "/api/search/":
            return httpx.Response(200, json={"results": [{"id": 1234,
                "release_date": catalog_date}]})
        assert request.url.path == "/api/1234/"
        return httpx.Response(200, json=source.page)

    def run():
        return select_nasa_article(source.core, source.options, {"records": []}, source.policy,
            now=STAMP, transport=httpx.MockTransport(handler), checkpoint=True)

    assert run() is None
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs").data["head_seen"] == []
    assert not (source.root / "data/server-sources").exists()
    # Page API исправил ошибочный год: повторный GET разрешён в следующий цикл,
    # однако прежние отказы прав, ссылок, типа, credits и давности сохраняются.
    source.page["release_date"] = (
        "2024-01-01T00:00:00+00:00" if other_error == "stale" else catalog_date
    )
    result = run()
    assert (result is not None) is (other_error is None)
    assert seen == ["/api/search/", "/api/1234/"] * 2
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs").data["head_seen"] == [1234]
    assert bool(list((source.root / "data/server-sources").glob("*.local.json"))) is (
        other_error is None
    )
    assert not cycle[3] and not cycle[4]


@pytest.mark.parametrize("bad_date", ["2027-01-01T00:00:00", "not-a-date"])
def test_invalid_full_page_date_is_not_deferred_as_an_aware_future(source, cycle, bad_date):
    source.core.db = cycle[1].db
    catalog_date = source.page["release_date"]
    source.page["release_date"] = bad_date
    seen = []

    def handler(request):
        assert request.method == "GET"
        seen.append(request.url.path)
        if request.url.path == "/api/search/":
            return httpx.Response(200, json={"results": [{"id": 1234,
                "release_date": catalog_date}]})
        assert request.url.path == "/api/1234/"
        return httpx.Response(200, json=source.page)

    transport = httpx.MockTransport(handler)
    for _ in range(2):
        assert select_nasa_article(source.core, source.options, {"records": []}, source.policy,
            now=STAMP, transport=transport, checkpoint=True) is None
        with source.core.db.session() as session:
            assert session.get(Cursor, "server_nasa_svs").data["head_seen"] == [1234]
    assert seen == ["/api/search/", "/api/1234/", "/api/search/"]
    assert not (source.root / "data/server-sources").exists()
    assert not cycle[3] and not cycle[4]
