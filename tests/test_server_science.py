"""CC BY источники: полный текст, неизменяемые доказательства и общий дедуп."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bs4 import BeautifulSoup

from max_postiz_agent import article_sources
from max_postiz_agent.article_sources import source_spec
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Cursor
from max_postiz_agent.reddit import text_hash
from max_postiz_agent.server_science import ADAPTERS, select_science_article

pytest_plugins = ["test_article_cycle"]
STAMP = datetime(2026, 10, 22, tzinfo=UTC).timestamp()
TITLE = "Young stars reshape a distant galaxy"
TEXT = (
    "Researchers observe young stars and explain how their light reshapes the surrounding gas. "
    "The measurements reveal an evolving stellar birthplace in a distant galaxy. "
)


@pytest.fixture(params=["noirlab_iotw", "esahubble_potw"])
def source(request, tmp_path, monkeypatch):
    source_id = request.param
    spec, adapter = source_spec(source_id), ADAPTERS[source_id]
    ident = "iotw2642a" if source_id == "noirlab_iotw" else "potw2642a"
    path = (
        "/public/images/" + ident + "/" if source_id == "noirlab_iotw" else "/images/" + ident + "/"
    )
    url = "https://" + spec["host"] + path
    rights = {
        "verified": True,
        "allow_storage": True,
        "allow_derivative": True,
        "allow_full_repost": True,
        "evidence_ref": spec["rights_evidence"],
    }
    settings = SimpleNamespace(root=tmp_path, source=lambda _: {"enabled": True, "rights": rights})
    core = SimpleNamespace(settings=settings)
    monkeypatch.setattr("max_postiz_agent.server_science._used_narratives", lambda *_: [])
    options = {
        "authors": [],
        "keywords": [],
        "min_rating": 0,
        "max_candidates": 3,
        "max_words": 400,
    }
    policy = {
        "source_id": source_id,
        "allowed_hosts": [spec["host"]],
        "rights_evidence": rights["evidence_ref"],
        "max_source_age_hours": 8760,
    }
    terms = (
        f'<html lang="en"><div id="editable-content"><h1>Usage of web texts</h1>'
        f"<p>{adapter['publisher']} texts of press releases, pictures of the week and captions "
        "are released under the Creative Commons Attribution 4.0 International License.</p>"
        '<a href="http://creativecommons.org/licenses/by/4.0/">License</a></div></html>'
    )
    terms_text = " ".join(
        BeautifulSoup(terms, "html.parser")
        .select_one("#editable-content")
        .get_text(" ", strip=True)
        .split()
    )
    semantic_hash = hashlib.sha256(terms_text.encode()).hexdigest()
    monkeypatch.setitem(article_sources._SOURCES[source_id], "rights_text_sha256", semantic_hash)
    spec["rights_text_sha256"] = semantic_hash

    def catalog(cards=None, following=None):
        cards = cards or [(path, TITLE, "21 October 2026")]
        return (
            '<html lang="en"><div class="news-list">'
            + "".join(
                f'<a href="{href}"><div class="news-id">{adapter["kind"]}</div>'
                f'<div class="news-title">{title}</div><div class="news-teaser"><strong>{date}</strong>: partial summary</div></a>'
                for href, title, date in cards
            )
            + "</div>"
            + (
                f'<ul class="pagination"><li><a href="{following}">Next</a></li></ul>'
                if following
                else ""
            )
            + "</html>"
        )

    def detail(ident=ident, title=TITLE, body=None, date=None):
        body = body or (
            f'<p>{TEXT}<a href="https://science.example.org/data/">Full dataset</a></p>'
            "<p>Another complete paragraph follows the first observation.</p>"
        )
        if source_id == "esahubble_potw":
            body = (
                '<div style="margin-top: 10px">'
                + body
                + '<p>[Image description: bright stars appear within dark clouds.]</p><h3>Links</h3><ul><li><a href="/videos/potw2642a/">Pan</a></li></ul></div>'
            )
        else:
            body = "<p>" + body + "</p>"
        date = date or (
            "Oct. 21, 2026, noon" if source_id == "noirlab_iotw" else "21 October 2026, 06:00"
        )
        return (
            '<html lang="en"><div class="left-column">'
            f'<h1>{title}</h1><div class="archive-image"><a href="https://media.invalid/photo.jpg"><img alt="Never downloaded"/></a></div>'
            + body
            + '<strong>Credit:</strong><div class="credit">'
            + adapter["publisher"]
            + "/Jane Scientist</div>"
            f'<div class="copyright"><a href="{spec["rights_evidence"]}">Usage of {adapter["publisher"]} Images and Videos</a></div></div>'
            f'<div class="right-column"><table><tr><th>Id:</th><td>{ident}</td></tr>'
            f"<tr><th>Release date:</th><td>{date}</td></tr></table></div>"
            "<aside>Related text is never part of the article.</aside></html>"
        )

    pages = {
        adapter["catalog"]: catalog(),
        path: detail(),
        urlsplit_path(spec["rights_evidence"]): terms,
    }
    requests = []

    def handler(request):
        requests.append(str(request.url))
        assert (
            request.method == "GET" and request.url.host == spec["host"] and not request.url.query
        )
        assert request.url.path in pages
        return httpx.Response(
            200, text=pages[request.url.path], headers={"content-type": "text/html; charset=utf-8"}
        )

    def run(ledger=None, transport=None, checkpoint=False, now=STAMP):
        return select_science_article(
            core,
            source_id,
            options,
            ledger or {"records": []},
            policy,
            now=now,
            transport=transport or httpx.MockTransport(handler),
            checkpoint=checkpoint,
        )

    return SimpleNamespace(
        core=core,
        options=options,
        policy=policy,
        rights=rights,
        spec=spec,
        source_id=source_id,
        adapter=adapter,
        root=tmp_path,
        url=url,
        path=path,
        ident=ident,
        pages=pages,
        run=run,
        requests=requests,
        catalog=catalog,
        detail=detail,
        handler=handler,
    )


def urlsplit_path(url):
    from urllib.parse import urlsplit

    return urlsplit(url).path


def test_full_narrative_links_credits_and_rights_saved_without_media(source):
    article = source.run()
    assert set(article) == {"url", "title", "original", "published_at"}
    assert article["url"] == source.url and article["published_at"] == STAMP - 86400
    assert TEXT.strip() in article["original"]
    assert "[Full dataset](https://science.example.org/data/)" in article["original"]
    assert "Another complete paragraph" in article["original"]
    assert (
        "partial summary" not in article["original"] and "Related text" not in article["original"]
    )
    assert "Never downloaded" not in article["original"]
    if source.source_id == "esahubble_potw":
        assert (
            "Image description" in article["original"]
            and "[Pan](https://esahubble.org/videos/potw2642a/)" in article["original"]
        )
    assert len(source.requests) == 3 and all("media.invalid" not in u for u in source.requests)
    proofs = list((source.root / "data/server-sources").glob("*.local.json"))
    assert len(proofs) == 1
    proof = json.loads(proofs[0].read_text(encoding="utf-8"))
    assert proof["article"] == article and "Jane Scientist" in proof["credit"]
    assert (
        proof["date_precision"] == "day"
        and proof["links"][0]["url"] == "https://science.example.org/data/"
    )
    for kind in ("page", "catalog", "rights"):
        assert hashlib.sha256(proof[kind + "_html"].encode()).hexdigest() == proof["hashes"][kind]


@pytest.mark.parametrize(
    "bad",
    [
        "unsafe_href",
        "missing_href",
        "duplicate_href",
        "missing_credit",
        "unexpected_block",
        "wrong_id",
        "copyright_exception",
        "wrong_date",
        "wrong_language",
    ],
)
def test_incomplete_unsafe_or_unlicensed_detail_is_skipped_without_terms_get(source, bad):
    page = source.pages[source.path]
    if bad == "unsafe_href":
        page = page.replace('href="https://science.example.org/data/"', 'href="javascript:steal()"')
    if bad == "missing_href":
        page = page.replace('href="https://science.example.org/data/"', "")
    if bad == "duplicate_href":
        page = page.replace(
            'href="https://science.example.org/data/"',
            'href="javascript:steal()" href="https://science.example.org/data/"',
        )
    if bad == "missing_credit":
        page = page.replace("<strong>Credit:</strong>", "")
    if bad == "unexpected_block":
        page = page.replace(
            "<strong>Credit:</strong>",
            "<section>Silently omitted scientific text</section><strong>Credit:</strong>",
        )
    if bad == "wrong_id":
        page = page.replace(f"<td>{source.ident}</td>", "<td>other2620a</td>")
    if bad == "copyright_exception":
        page = page.replace(TEXT, TEXT + " All rights reserved. ")
    if bad == "wrong_date":
        page = page.replace("21, 2026", "20, 2026").replace(
            "21 October 2026, 06:00", "20 October 2026, 06:00"
        )
    if bad == "wrong_language":
        page = page.replace('lang="en"', 'lang="es"')
    source.pages[source.path] = page
    assert source.run() is None and len(source.requests) == 2
    assert not (source.root / "data/server-sources").exists()


@pytest.mark.parametrize(
    "bad",
    ["not_verified", "no_derivative", "no_full_repost", "wrong_evidence", "wrong_policy", "rating"],
)
def test_rights_and_policy_are_required_before_network(source, bad):
    if bad == "not_verified":
        source.rights["verified"] = False
    if bad == "no_derivative":
        source.rights["allow_derivative"] = False
    if bad == "no_full_repost":
        source.rights["allow_full_repost"] = False
    if bad == "wrong_evidence":
        source.rights["evidence_ref"] = "some other permission"
    if bad == "wrong_policy":
        source.policy["allowed_hosts"] = ["foreign.invalid"]
    if bad == "rating":
        source.options["min_rating"] = 1
    with pytest.raises(AgentError):
        source.run()
    assert source.requests == []


@pytest.mark.parametrize("bad", ["grant_changed", "grant_moved_to_footer", "license_href_changed"])
def test_changed_license_or_bare_footer_license_never_proves_text_rights(source, bad):
    path = urlsplit_path(source.spec["rights_evidence"])
    if bad == "grant_changed":
        source.pages[path] = source.pages[path].replace("are released", "are not released")
    if bad == "grant_moved_to_footer":
        source.pages[path] = source.pages[path].replace('id="editable-content"', 'id="footer"')
    if bad == "license_href_changed":
        source.pages[path] = source.pages[path].replace("licenses/by/4.0/", "licenses/by-nd/4.0/")
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED"):
        source.run()
    assert len(source.requests) == 3 and not (source.root / "data/server-sources").exists()


@pytest.mark.parametrize("status", [302, 401, 403, 429, 503])
def test_network_errors_redirects_and_challenges_stop_without_retry(source, status):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://foreign.invalid/"})

    code = (
        "SCIENCE_SOURCE_ACCESS_BLOCKED"
        if status in {401, 403, 429}
        else "SCIENCE_SOURCE_UNAVAILABLE"
    )
    with pytest.raises(AgentError, match=code):
        source.run(transport=httpx.MockTransport(handler))
    assert len(seen) == 1


def test_timeout_is_not_a_known_skip_or_retry(source):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        raise httpx.ReadTimeout("unknown response")

    with pytest.raises(AgentError, match="SCIENCE_SOURCE_UNAVAILABLE"):
        source.run(transport=httpx.MockTransport(handler))
    assert len(seen) == 1


@pytest.mark.parametrize("kind", ["cancelled_url", "nasa_hash", "cross_source_overlap"])
def test_all_historical_sources_including_cancellations_share_dedup(source, monkeypatch, kind):
    ledger = (
        {"records": [{"url": source.url, "state": "cancelled"}]}
        if kind == "cancelled_url"
        else {"records": []}
    )
    if kind != "cancelled_url":
        article = source.run()
        source.requests.clear()
        if kind == "nasa_hash":
            ledger = {
                "records": [
                    {
                        "url": "https://svs.gsfc.nasa.gov/1234/",
                        "original_hash": text_hash(article["original"]),
                    }
                ]
            }
        else:
            monkeypatch.setattr(
                "max_postiz_agent.server_science._used_narratives", lambda *_: [article["original"]]
            )
    assert source.run(ledger) is None
    assert len(source.requests) == (1 if kind == "cancelled_url" else 2)


def test_author_keywords_and_full_length_are_applied_to_the_complete_article(source):
    source.options.update(authors=["Jane Scientist"], keywords=["stellar birthplace"])
    assert source.run() is not None
    source.options["authors"] = ["Absent author"]
    assert source.run() is None
    source.options.update(authors=[], keywords=["missing phrase"])
    assert source.run() is None
    source.options.update(keywords=[], max_words=5)
    assert source.run() is None


@pytest.mark.parametrize("bad", ["foreign_host", "query", "credentials", "unsafe_path", "bad_next"])
def test_catalog_cannot_turn_untrusted_links_into_get_targets(source, bad):
    page = source.pages[source.adapter["catalog"]]
    if bad == "foreign_host":
        page = page.replace(f'href="{source.path}"', f'href="https://foreign.invalid{source.path}"')
    if bad == "query":
        page = page.replace(f'href="{source.path}"', f'href="{source.path}?token=secret"')
    if bad == "credentials":
        page = page.replace(
            f'href="{source.path}"', f'href="https://user:pass@{source.spec["host"]}{source.path}"'
        )
    if bad == "unsafe_path":
        page = page.replace(f'href="{source.path}"', 'href="/copyright/"')
    if bad == "bad_next":
        page = source.catalog(following="https://foreign.invalid/page/2/")
    source.pages[source.adapter["catalog"]] = page
    with pytest.raises(AgentError):
        source.run()
    assert len(source.requests) == 1


@pytest.mark.parametrize("where", ["catalog", "detail"])
def test_future_release_is_deferred_without_losing_the_source_or_weakening_age(
    source, cycle, where
):
    source.core.db = cycle[1].db
    if where == "catalog":
        source.pages[source.adapter["catalog"]] = source.catalog(
            cards=[(source.path, TITLE, "23 October 2026")]
        )
    else:
        source.pages[source.path] = source.detail(
            date=(
                "Oct. 23, 2026, noon"
                if source.source_id == "noirlab_iotw"
                else "23 October 2026, 06:00"
            )
        )
    assert source.run(checkpoint=True) is None
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_science_" + source.source_id).data["head_seen"] == []
    source.pages[source.adapter["catalog"]] = source.catalog()
    source.pages[source.path] = source.detail()
    assert source.run(checkpoint=True) is not None
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_science_" + source.source_id).data["head_seen"] == [
            source.url
        ]
    assert not cycle[3] and not cycle[4]


def test_source_checkpoint_advances_past_a_bounded_rejected_head_and_preserves_nasa(source, cycle):
    source.core.db = cycle[1].db
    source.options["max_candidates"] = 2
    prefix = "/public/images/" if source.source_id == "noirlab_iotw" else "/images/"
    ids = [source.ident.replace("2642", f"26{i:02}") for i in (40, 41, 42)]
    paths = [prefix + i + "/" for i in ids]
    source.pages[source.adapter["catalog"]] = source.catalog(
        cards=[(p, TITLE, "21 October 2026") for p in paths[:2]],
        following=source.adapter["catalog"] + "page/2/",
    )
    source.pages[source.adapter["catalog"] + "page/2/"] = source.catalog(
        cards=[(paths[2], TITLE, "21 October 2026")]
    )
    for i, path in enumerate(paths):
        source.pages[path] = source.detail(ident=ids[i])
        if i < 2:
            source.pages[path] = source.pages[path].replace("<strong>Credit:</strong>", "")
    old = {"offset": 123, "selection_hash": "old-nasa", "head_seen": [1], "offset_seen": []}
    with source.core.db.session.begin() as session:
        session.add(Cursor(id="server_nasa_svs", data=copy.deepcopy(old)))
    assert source.run(checkpoint=True) is None
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_science_" + source.source_id).data["page"] == 2
        assert session.get(Cursor, "server_nasa_svs").data == old
    source.requests.clear()
    assert source.run(checkpoint=True)["url"].endswith(paths[2])
    assert len(source.requests) == 4
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_nasa_svs").data == old


def test_readonly_selection_does_not_create_any_source_cursor(source, cycle):
    source.core.db = cycle[1].db
    assert source.run() is not None
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_science_" + source.source_id) is None


def test_hubble_next_outside_numbered_pagination_is_followed(source):
    from max_postiz_agent.server_science import _catalog

    html = source.catalog(following=source.adapter["catalog"] + "page/2/")
    html = html.replace('<ul class="pagination"><li>', '<div class="prev-next">').replace(
        "</li></ul>", "</div>"
    )
    assert _catalog(html.encode(), source.source_id, 1)[1] == 2


def test_malformed_durable_checkpoint_blocks_before_network(source, cycle):
    source.core.db = cycle[1].db
    with source.core.db.session.begin() as session:
        session.add(Cursor(id="server_science_" + source.source_id, data=["not a checkpoint"]))
    with pytest.raises(AgentError, match="SCIENCE_CHECKPOINT_INVALID"):
        source.run(checkpoint=True)
    assert source.requests == []


def test_response_size_is_bounded_before_html_or_following_requests(source):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200, content=b"X" * (2 * 1024 * 1024 + 1), headers={"content-type": "text/html"}
        )

    with pytest.raises(AgentError, match="ARTICLE_SOURCE_TOO_LARGE"):
        source.run(transport=httpx.MockTransport(handler))
    assert len(seen) == 1


def test_success_status_with_bot_challenge_is_not_a_source_or_retry(source):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            text='<html lang="en">Verify you are not a robot</html>',
            headers={"content-type": "text/html"},
        )

    with pytest.raises(AgentError, match="SCIENCE_SOURCE_ACCESS_BLOCKED"):
        source.run(transport=httpx.MockTransport(handler))
    assert len(seen) == 1


def test_private_nasa_excluded_hash_cannot_be_reused_via_another_publisher(source):
    article = source.run()
    source.requests.clear()
    path = source.root / "data/nasa-source-exclusions.local.json"
    path.write_text(
        json.dumps(
            {
                "excluded": [
                    {
                        "url": "https://svs.gsfc.nasa.gov/1234/",
                        "original_hash": text_hash(article["original"]),
                        "reason": "Previously verified conflicting facts; source retained in audit",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert source.run() is None
    assert len(source.requests) == 2


def test_corrupt_shared_private_exclusion_blocks_before_any_network(source):
    directory = source.root / "data"
    directory.mkdir()
    (directory / "nasa-source-exclusions.local.json").write_text('{"excluded": "corrupt"}')
    with pytest.raises(AgentError, match="NASA_EXCLUSIONS_INVALID"):
        source.run()
    assert source.requests == []


@pytest.mark.parametrize("status", [200, 401, 403, 429])
def test_explicit_access_block_is_durable_without_resetting_source_or_peer_cursor(
    source, cycle, status
):
    source.core.db = cycle[1].db
    cursor_id = "server_science_" + source.source_id
    peer_id = "server_science_" + (
        "esahubble_potw" if source.source_id == "noirlab_iotw" else "noirlab_iotw"
    )
    old = {
        "page": 2,
        "head_seen": [source.url],
        "page_seen": [],
        "selection_hash": fingerprint({"options": source.options, "policy": source.policy}),
    }
    with source.core.db.session.begin() as session:
        session.add(
            Cursor(
                id=cursor_id,
                data=copy.deepcopy(old),
                last_success=1234,
                status="source_metadata_checkpoint",
            )
        )
        session.add(
            Cursor(id=peer_id, data={"preserved": "peer"}, status="source_metadata_checkpoint")
        )
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            status,
            text='<html lang="en">Verify you are not a robot</html>',
            headers={"content-type": "text/html"},
        )

    for _ in range(2):
        with pytest.raises(AgentError, match="SCIENCE_SOURCE_ACCESS_BLOCKED"):
            source.run(transport=httpx.MockTransport(handler), checkpoint=True)
        source.options["max_words"] += 1
    assert len(seen) == 1
    with source.core.db.session() as session:
        row = session.get(Cursor, cursor_id)
        assert row.status == "access_blocked" and row.data == old and row.last_success == 1234
        peer = session.get(Cursor, peer_id)
        assert peer.status == "source_metadata_checkpoint" and peer.data == {"preserved": "peer"}


@pytest.mark.parametrize("error", ["timeout", "503"])
def test_temporary_network_failure_does_not_persist_an_access_prohibition(source, cycle, error):
    source.core.db = cycle[1].db
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if error == "timeout":
            raise httpx.ReadTimeout("temporary failure")
        return httpx.Response(503)

    with pytest.raises(AgentError, match="SCIENCE_SOURCE_UNAVAILABLE"):
        source.run(transport=httpx.MockTransport(handler), checkpoint=True)
    assert len(seen) == 1
    with source.core.db.session() as session:
        assert session.get(Cursor, "server_science_" + source.source_id) is None


@pytest.mark.parametrize(
    "field",
    [
        "page_html",
        "catalog_html",
        "rights_html",
        "credit",
        "links",
        "source_id",
        "rights_evidence",
        "rights_text_sha256",
        "narrative_html",
        "rights_box_html",
        "rights_box_text",
        "article",
        "hashes",
        "extra_key",
        "missing_key",
        "future_selected_at",
        "nonfinite_selected_at",
        "negative_selected_at",
        "negative_count",
        "future_count",
        "boolean_count",
        "boolean_selected_at",
    ],
)
def test_reused_immutable_source_proof_rejects_every_tampered_field_without_repair(source, field):
    assert source.run() is not None
    path = next((source.root / "data/server-sources").glob("*.local.json"))
    proof = json.loads(path.read_text(encoding="utf-8"))
    if field in {
        "page_html",
        "catalog_html",
        "rights_html",
        "credit",
        "source_id",
        "rights_evidence",
        "rights_text_sha256",
        "rights_box_html",
        "rights_box_text",
    }:
        proof[field] += "tampered"
    elif field in {"links", "narrative_html"}:
        proof[field] = []
    elif field == "article":
        proof[field]["title"] += " changed"
    elif field == "hashes":
        proof[field]["page"] = "0" * 64
    elif field == "extra_key":
        proof["unbound_note"] = "not part of source proof"
    elif field == "missing_key":
        del proof["credit"]
    elif field == "future_selected_at":
        proof["selected_at"] = STAMP + 1
    elif field == "nonfinite_selected_at":
        proof["selected_at"] = float("nan")
    elif field == "negative_selected_at":
        proof["selected_at"] = -1
    elif field == "negative_count":
        proof["dedup_records"] = -1
    elif field == "future_count":
        proof["dedup_records"] = 1
    elif field == "boolean_count":
        proof["dedup_records"] = False
    elif field == "boolean_selected_at":
        proof["selected_at"] = False
    path.write_text(json.dumps(proof, ensure_ascii=False), encoding="utf-8")
    tampered = path.read_bytes()
    with pytest.raises(AgentError, match="SCIENCE_SOURCE_PROOF_CONFLICT"):
        source.run()
    assert path.read_bytes() == tampered


def test_older_proof_keeps_original_selection_time_and_record_count(source):
    ledger = {
        "records": [
            {"url": f"https://history.example/{i}/", "state": "cancelled"} for i in range(2)
        ]
    }
    assert source.run(ledger, now=STAMP + 3600) is not None
    path = next((source.root / "data/server-sources").glob("*.local.json"))
    original = path.read_bytes()
    ledger["records"].append({"url": "https://history.example/extra/", "state": "cancelled"})
    assert source.run(ledger, now=STAMP + 7200) is not None
    assert path.read_bytes() == original
    proof = json.loads(original)
    assert proof["selected_at"] == STAMP + 3600 and proof["dedup_records"] == 2


@pytest.mark.parametrize(
    "change",
    [
        "notice",
        "label",
        "attribute",
        "comment",
        "duplicate",
        "empty",
        "bad_href",
        "noncommercial",
        "no_derivatives",
        "personal_only",
    ],
)
def test_per_article_copyright_notice_cannot_hide_behind_the_standard_license_link(source, change):
    soup = BeautifulSoup(source.pages[source.path], "html.parser")
    box = soup.select_one(".left-column div.copyright")
    notice = "Text copyright Third Party. All rights reserved."
    if change == "notice":
        box.append(notice)
    elif change == "noncommercial":
        box.append("Use is permitted for noncommercial purposes only.")
    elif change == "no_derivatives":
        box.append("No derivatives are permitted.")
    elif change == "personal_only":
        box.append("This text is for personal use only.")
    elif change == "label":
        box.a.string = notice
    elif change == "attribute":
        box.a.string = "Copyright"
        box.a["title"] = notice
    elif change == "comment":
        from bs4 import Comment

        box.append(Comment(notice))
    elif change == "duplicate":
        box.insert_after(copy.deepcopy(box))
    elif change == "empty":
        box.clear()
    elif change == "bad_href":
        box.a["href"] = "javascript:bad()"
    source.pages[source.path] = str(soup)
    assert source.run() is None and len(source.requests) == 2
    assert not (source.root / "data/server-sources").exists()


@pytest.mark.parametrize("label", ["Copyright", "official_usage"])
def test_exact_benign_label_at_exact_license_url_does_not_trigger_a_false_rights_exception(
    source, label
):
    soup = BeautifulSoup(source.pages[source.path], "html.parser")
    box = soup.select_one(".left-column div.copyright")
    box.a.string = (
        "Copyright"
        if label == "Copyright"
        else "Usage of " + source.adapter["publisher"] + " Images and Videos"
    )
    source.pages[source.path] = str(soup)
    assert source.run() is not None
    proof = json.loads(
        next((source.root / "data/server-sources").glob("*.local.json")).read_text(encoding="utf-8")
    )
    assert proof["rights_box_text"] == box.get_text(" ", strip=True)
    assert "copyright" in proof["rights_box_html"]


@pytest.mark.parametrize("position", ["after_marker", "after_credit", "after_rights"])
@pytest.mark.parametrize(
    "notice",
    [
        "Text copyright Third Party. All rights reserved.",
        "This text is for personal use only.",
        "No derivatives are permitted.",
        "An unfamiliar note requires a new source layout review.",
    ],
)
def test_unknown_own_notice_anywhere_after_credit_marker_blocks_article(source, position, notice):
    soup = BeautifulSoup(source.pages[source.path], "html.parser")
    column = soup.select_one(".left-column")
    node = soup.new_tag("p")
    node.string = notice
    anchor = (
        column.find("strong", recursive=False)
        if position == "after_marker"
        else column.select_one("div.credit" if position == "after_credit" else "div.copyright")
    )
    anchor.insert_after(node)
    source.pages[source.path] = str(soup)
    assert source.run() is None and len(source.requests) == 2
    assert not (source.root / "data/server-sources").exists()


@pytest.mark.parametrize("position", ["after_marker", "after_credit", "after_rights"])
def test_known_tail_allows_only_whitespace_and_empty_br(source, position):
    soup = BeautifulSoup(source.pages[source.path], "html.parser")
    column = soup.select_one(".left-column")
    anchor = (
        column.find("strong", recursive=False)
        if position == "after_marker"
        else column.select_one("div.credit" if position == "after_credit" else "div.copyright")
    )
    anchor.insert_after(soup.new_tag("br"))
    source.pages[source.path] = str(soup)
    assert source.run() is not None


@pytest.mark.parametrize("where", ["credit", "rights", "narrative"])
@pytest.mark.parametrize(
    "notice",
    [
        "This text is for personal use only.",
        "Personal-use-only.",
        "This material is only for personal use.",
        "This text is noncommercial.",
        "Non-commercial use only.",
        "Non commercial distribution only.",
        "No derivatives are permitted.",
        "NoDerivatives.",
        "No adaptations are permitted.",
        "No translations are permitted.",
        "Translations are not permitted.",
        "Adaptation is forbidden.",
        "Reproduction strictly prohibited.",
        "Redistribution is restricted.",
        "Reuse is not allowed.",
        "Not permitted: translations.",
        "Do not translate this text.",
        "Written permission is required.",
        "Translation requires prior permission.",
        "This text is under CC BY-NC 4.0.",
        "License: CC-BY-ND 4.0.",
    ],
)
def test_explicit_per_item_restriction_blocks_inside_credit_rights_and_narrative(
    source, where, notice
):
    soup = BeautifulSoup(source.pages[source.path], "html.parser")
    column = soup.select_one(".left-column")
    paragraph = soup.new_tag("p")
    paragraph.string = notice
    if where == "credit":
        column.select_one("div.credit").append(paragraph)
    elif where == "rights":
        column.select_one("div.copyright").append(paragraph)
    elif source.source_id == "esahubble_potw":
        column.find("div", style="margin-top: 10px", recursive=False).append(paragraph)
    else:
        column.find("strong", recursive=False).insert_before(paragraph)
    source.pages[source.path] = str(soup)
    assert source.run() is None and len(source.requests) == 2
    assert not (source.root / "data/server-sources").exists()
