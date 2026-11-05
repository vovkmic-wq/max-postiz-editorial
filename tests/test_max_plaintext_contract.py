"""Real local Hub/SQLite + in-process Postiz stubs; never models or delivery."""
import copy
import hashlib
import io
import json
import socket
from types import SimpleNamespace

from bs4 import BeautifulSoup
import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.browser_worker import _snapshot_request
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Draft, Story
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant, VariantRevision
from max_postiz_agent.max_bridge import MaxBridge
from max_postiz_agent.postiz import PostizClient

pytest_plugins = ["test_hub"]


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("NO_NETWORK_MODEL_OR_SEND_PERMITTED")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(ArticleQuality, "prepare", forbidden)


def source_body():
    # Match the real incident's lengths AND replacement offsets, with synthetic text.
    title = 'Звёздный ветер'
    prefix = (title + '\n\n' + 'Синтетическое научное наблюдение. ' * 12)[:132]
    core = prefix + '&quot;светило&quot;'
    footer = ('\n\nИсточник: https://science.example.org/fixture/\n'
              'Credit: Fixture A & B. CC BY 4.0, https://creativecommons.org/licenses/by/4.0/\n'
              'Перевод и адаптация.')
    filler = (' Данные и численные наблюдения сохранены буквально.' * 100)
    body = core + filler[:2476-len(core)-len(footer)] + footer
    assert len(body) == 2476 and body[132:138] == body[145:151] == '&quot;'
    return title, body


def core_stage(hub, monkeypatch, *, body_override=None, stage=True):
    core = hub
    title, body = source_body()
    if body_override is not None:
        body = body_override
    core.bridge.key = 'fixture-only-bridge'
    core.publishing['postiz']['browser_bridge_enabled'] = True
    (core.settings.root / 'config/publishing.json').write_text(json.dumps(core.publishing))
    picture = io.BytesIO()
    Image.new('RGB', (1600, 900), 'navy').save(picture, format='JPEG')
    raw_image = picture.getvalue()
    digest = hashlib.sha256(raw_image).hexdigest()
    image_dir = core.settings.root / core.settings.app['media_dir']
    image_dir.mkdir(parents=True, exist_ok=True)
    image_file = image_dir / (digest + '.jpg')
    image_file.write_bytes(raw_image)
    media = {'path': image_file.relative_to(core.settings.root).as_posix(), 'sha256': digest, 'mime': 'image/jpeg'}
    illustration = {'contract': 'article-illustrated-v2', 'credit': 'Fixture A & B. CC BY 4.0',
                    'media': media, 'width': 1600, 'height': 900, 'bytes': len(raw_image)}
    with core.db.session.begin() as session:
        target = session.get(Target, 'max')
        target.route = 'max_browser'
        target.options = {'max_chars': 3500, 'postiz_integration_id': 'fixture-integration'}
        story = Story(text_hash=fingerprint({'fixture': body}), title=title, text=body)
        session.add(story)
        session.flush()
        draft = Draft(channel_id='main', story_id=story.id, current={'title': title, 'body': body},
                      config_id=core.settings.hash, state='review_pending')
        session.add(draft)
        session.flush()
        variant = Variant(draft_id=draft.id, target_id=target.id, title=title, body=body, media=[media], sources=[],
                          source_revision=1, config_hash=core.settings.hash, binding_hash=target.binding_hash,
                          metadata_json={'origin': 'hourly_article_test', 'presentation_contract': 'article-illustrated-v2',
                                         'illustration': illustration, 'illustration_proof_hash': fingerprint(illustration)})
        session.add(variant)
        session.flush()
        variant_id = variant.id
        core._history(session, variant, target, 'article_quality')
        before = copy.deepcopy(core.snapshot(variant, target))
    records, captured = {}, []
    def request(method, path, **kwargs):
        assert method == 'POST' and path == '/posts'
        payload = copy.deepcopy(kwargs['json'])
        captured.append(payload)
        post = payload['posts'][0]
        value = post['value'][0]
        records['fixture-post'] = {'id': 'fixture-post', 'content': value['content'], 'image': value['image'],
            'publishDate': payload['date'], 'state': 'DRAFT', 'childrenPost': [], 'intervalInDays': None,
            'group': 'fixture-group', 'integration': {'id': 'fixture-integration'}, 'settings': json.dumps(post['settings'])}
        return [{'postId': 'fixture-post'}]
    core.postiz.client.close()
    core.postiz.base = 'http://127.0.0.1:19999/public/v1'
    core.postiz.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw_image, headers={'content-type': 'image/jpeg'})))
    monkeypatch.setattr(core.postiz, 'request', request)
    monkeypatch.setattr(core.postiz, 'posts', lambda *args: copy.deepcopy(list(records.values())))
    monkeypatch.setattr(core.postiz, 'get_post_fresh', lambda ident: core.postiz.get_post(ident))
    monkeypatch.setattr(core.postiz, 'integrations', lambda: [{'id': 'fixture-integration', 'identifier': 'max-browser',
        'disabled': False, 'refreshNeeded': False, 'accountId': '-123'}])
    monkeypatch.setattr(core.postiz, 'upload', lambda *_: {'id': 'fixture-image', 'path': '/uploads/fixture.jpg'})
    monkeypatch.setattr(core.publisher, 'publish', lambda *_args, **_kwargs: pytest.fail('SEND_FORBIDDEN'))
    if not stage:
        return core, before, None, captured, records
    assert core.stage_remote(variant_id) == 'fixture-post'
    with core.db.session() as session:
        after = copy.deepcopy(core.snapshot(session.get(Variant, variant_id), session.get(Target, 'max')))
        history = list(session.scalars(select(VariantRevision).where(VariantRevision.variant_id == variant_id)
                                      .order_by(VariantRevision.revision)))
        assert [row.actor for row in history] == ['article_quality', 'postiz_bridge']
        assert [row.revision for row in history] == [1, 2]
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == session.query(DispatchAttempt).count() == 0
    return core, before, after, captured, records


def test_real_hub_exact_prepared_snapshot_survives_postiz_stage(hub, monkeypatch):
    core, before, after, captured, _ = core_stage(hub, monkeypatch)
    assert len(captured) == 1
    assert before['media'] == after['media'] and before['title'] == after['title']
    # This is the real stage_reserve comparison, with no unescape/normalization concession.
    assert HourlyArticleCycle._reviewed_fields(after) == HourlyArticleCycle._reviewed_fields(before)
    assert len(before['body']) == len(after['body']) == 2476
    assert after['body'].count('&quot;') == 2
    expected, presentation = _snapshot_request(core.settings, 'fixture-request', after, '-123')
    assert expected['text'] == before['body'] and presentation['title_text'] == before['title']
    assert presentation['title_format'] == 'bold' and presentation['presentation_contract'] == 'article-illustrated-v2'
    assert expected['attachment']['type'] == 'browser-image'
    assert expected['attachment']['payload']['sha256'] == before['media'][0]['sha256']
    assert 'Перевод и адаптация.' in after['body'] and 'CC BY 4.0' in after['body']
    assert 'Архивный научный материал.' not in after['body']
    html = captured[0]['posts'][0]['value'][0]['content']
    assert BeautifulSoup(html, 'html.parser').find(True) is None


@pytest.mark.parametrize('plain', [
    '&quot;имя&quot;', '&amp;quot; вложенный литерал', '&#34; &#x22; &nbsp;',
    '<script>alert(1)</script> и <img src="http://untrusted.invalid/pixel">',
    'A < B > C & R&D', 'Обычные "кавычки" и апостроф\' без разметки',
    'Первая строка\n\nТретья строка, Привет 🌌 и неразрывный\u00a0пробел',
])
@pytest.mark.parametrize('kind', ['draft', 'schedule'])
def test_max_transport_plaintext_is_exact_after_single_html_parse(monkeypatch, plain, kind):
    client = PostizClient({})
    captured = []
    monkeypatch.setattr(client, 'request', lambda *a, **kw: (captured.append(kw['json']), [{'postId': 'fixture'}])[1])
    target = SimpleNamespace(platform='max-browser', destination='fixture', options={})
    try:
        client.create(target, plain, [], kind=kind)
    finally:
        client.close()
    payload = captured[0]
    wire = payload['posts'][0]['value'][0]['content']
    remote = {'content': wire, 'image': [], 'childrenPost': [], 'publishDate': payload['date'],
              'state': 'DRAFT' if kind == 'draft' else 'QUEUE', 'group': 'fixture', 'integration': {'id': 'fixture'}}
    assert MaxBridge.normalize(remote)[0] == plain
    assert BeautifulSoup(wire, 'html.parser').find(True) is None


def test_non_max_postiz_routes_are_not_changed(monkeypatch):
    client = PostizClient({})
    captured = []
    monkeypatch.setattr(client, 'request', lambda *a, **kw: (captured.append(kw['json']), [{'postId': 'fixture'}])[1])
    text = '<b>legacy route</b> &quot; unchanged'
    try:
        client.create(SimpleNamespace(platform='telegram', destination='fixture', options={}), text, [])
    finally:
        client.close()
    assert captured[0]['posts'][0]['value'][0]['content'] == text


def test_staged_bridge_revision_and_plain_hash_advance_atomically(hub, monkeypatch):
    core, before, after, _, _ = core_stage(hub, monkeypatch)
    with core.db.session() as session:
        draft = session.get(RemoteDraft, after["variant_id"])
        assert after["revision"] == before["revision"] + 1 == draft.synced_revision
        assert draft.remote_hash == fingerprint({"body": before["body"]})
    assert core.sync_remote(after["variant_id"])["state"] == "unchanged"


def sync_capture(core, ident):
    with core.db.session() as session:
        variant = session.get(Variant, ident)
        target = session.get(Target, variant.target_id)
        return core.bridge.capture_draft_sync(session, variant, target), target


@pytest.mark.parametrize("change", ["revision", "body", "remote", "bridge", "target"])
def test_explicit_sync_rejects_local_race_even_if_remote_unchanged(hub, monkeypatch, change):
    core, _, after, _, records = core_stage(hub, monkeypatch)
    ident = after["variant_id"]
    expected, target = sync_capture(core, ident)
    with core.db.session.begin() as session:
        if change == "revision":
            session.get(Variant, ident).revision += 1
        elif change == "body":
            session.get(Variant, ident).body += " local edit"
        elif change == "remote":
            session.get(RemoteDraft, ident).correlation = "concurrent-correlation"
        elif change == "bridge":
            session.get(BridgePost, "fixture-post").group_id = "concurrent-group"
        else:
            session.get(Target, "max").destination = "concurrent-target"
    from max_postiz_agent.config import AgentError
    with pytest.raises(AgentError, match="REMOTE_EDIT_CONFLICT"):
        core.bridge.import_post(records["fixture-post"], target, expected_local=expected)


@pytest.mark.parametrize("change", ["body", "image", "date", "settings"])
def test_body_identical_remote_projection_changes_never_hide_as_unchanged(hub, monkeypatch, change):
    core, _, after, _, records = core_stage(hub, monkeypatch)
    remote = records["fixture-post"]
    if change == "body":
        remote["content"] += " changed"
    elif change == "image":
        remote["image"][0]["id"] = "different-asset"
    elif change == "date":
        remote["publishDate"] = "2030-01-01T00:00:00+00:00"
    else:
        settings = json.loads(remote["settings"])
        settings["editor_metadata"] = "changed"
        remote["settings"] = json.dumps(settings)
    result = core.sync_remote(after["variant_id"])
    assert result["state"] == "updated" and result["revision"] == after["revision"] + 1
    with core.db.session() as session:
        assert session.get(RemoteDraft, after["variant_id"]).synced_revision == result["revision"]
        assert session.query(VariantRevision).count() == 3
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


@pytest.mark.parametrize("field,value", [("id", "foreign-post"), ("group", "foreign-group"),
    ("integration", {"id": "foreign-integration"}), ("state", "QUEUE"),
    ("settings", json.dumps({"hub_variant_id": "foreign-variant"})),
    ("settings", json.dumps({"hub_correlation": "foreign-correlation"}))])
def test_explicit_sync_rejects_changed_remote_binding(hub, monkeypatch, field, value):
    core, _, after, _, records = core_stage(hub, monkeypatch)
    expected, target = sync_capture(core, after["variant_id"])
    remote = copy.deepcopy(records["fixture-post"])
    remote[field] = value
    from max_postiz_agent.config import AgentError
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_BINDING_CHANGED"):
        core.bridge.import_post(remote, target, expected_local=expected)
    with core.db.session() as session:
        assert session.get(Variant, after["variant_id"]).revision == after["revision"]


def test_stale_historical_synced_revision_is_not_auto_migrated(hub, monkeypatch):
    core, _, after, _, _ = core_stage(hub, monkeypatch)
    ident = after["variant_id"]
    with core.db.session.begin() as session:
        session.get(RemoteDraft, ident).synced_revision -= 1
    from max_postiz_agent.config import AgentError
    with pytest.raises(AgentError, match="REMOTE_EDIT_CONFLICT"):
        core.sync_remote(ident)
    with core.db.session() as session:
        assert session.get(RemoteDraft, ident).synced_revision == after["revision"] - 1


@pytest.mark.parametrize("remote_change", ["unchanged", "edited", "missing", "malformed"])
def test_retirement_poll_preserves_tombstone_and_processes_fresh_posts(hub, monkeypatch, remote_change):
    from max_postiz_agent.db import Audit
    from max_postiz_agent.preapproval_retirement import _id
    core, _, after, _, records = core_stage(hub, monkeypatch)
    ident = after["variant_id"]
    with core.db.session.begin() as session:
        session.add(Audit(id=_id(ident, "claim"), actor="fixture", action="preapproval_retirement_claim", entity=ident))
        before_link = core.bridge._row_view(session.get(BridgePost, "fixture-post"))
        before_variant = core.bridge._row_view(session.get(Variant, ident))
    fresh = copy.deepcopy(records["fixture-post"])
    fresh["id"], fresh["group"], fresh["settings"] = "fresh-post", "fresh-group", "{}"
    records[fresh["id"]] = fresh
    if remote_change == "edited":
        records["fixture-post"]["content"] += " remote edit"
    elif remote_change == "missing":
        del records["fixture-post"]
    elif remote_change == "malformed":
        del records["fixture-post"]["image"]
    media_calls = []
    original_media = core.bridge._media
    monkeypatch.setattr(core.bridge, "_media", lambda *args: (media_calls.append(args), original_media(*args))[1])
    core.bridge.sync(force=True)
    with core.db.session() as session:
        assert core.bridge._row_view(session.get(BridgePost, "fixture-post")) == before_link
        assert core.bridge._row_view(session.get(Variant, ident)) == before_variant
        assert session.get(BridgePost, "fresh-post") is not None
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    assert len(media_calls) == 1  # only fresh-post; tombstone skipped before media/schema parsing
    token = core.bridge._seal({"kind": "channel", "target": "max", "destination": "-123", "expires": 9e10})
    assert core.bridge.status(token, "fixture-integration", "fixture-post")["state"] == "cancelled"
    monkeypatch.setattr(core.postiz, "get_post", lambda *_: pytest.fail("TOMBSTONE_REMOTE_GET_FORBIDDEN"))
    assert core.bridge.submit(token, "fixture-integration", "fixture-post")["state"] == "cancelled"


def test_retirement_blocks_new_lease_but_allows_exact_owner_cleanup(hub, monkeypatch):
    from max_postiz_agent.config import AgentError
    from max_postiz_agent.db import Audit
    from max_postiz_agent.preapproval_retirement import _id
    core, _, after, _, _ = core_stage(hub, monkeypatch)
    owner = core.bridge.mutation(["fixture-post"])["owner"]
    with core.db.session.begin() as session:
        session.add(Audit(id=_id(after["variant_id"], "claim"), actor="fixture", action="preapproval_retirement_claim",
                          entity=after["variant_id"]))
    with pytest.raises(AgentError, match="PREAPPROVAL_RETIRED_OR_RECONCILING"):
        core.bridge.mutation(["fixture-post"])
    core.bridge.mutation(["fixture-post"], release="foreign-owner")
    with core.db.session() as session:
        assert session.get(BridgePost, "fixture-post").mutation_owner == owner
    core.bridge.mutation(["fixture-post"], release=owner)
    with core.db.session() as session:
        assert session.get(BridgePost, "fixture-post").mutation_owner is None
        assert session.get(BridgePost, "fixture-post").mutation_until == 0


def test_duplicate_remote_relationship_blocks_background_import_before_media(hub, monkeypatch):
    from max_postiz_agent.config import AgentError
    core, _, after, _, records = core_stage(hub, monkeypatch)
    ident = after["variant_id"]
    with core.db.session.begin() as session:
        original = session.get(Variant, ident)
        duplicate = Variant(draft_id=original.draft_id, target_id="pikabu", body="fixture", revision=1,
                            source_revision=1, config_hash=original.config_hash, binding_hash=original.binding_hash)
        session.add(duplicate)
        session.flush()
        session.add(RemoteDraft(variant_id=duplicate.id, external_id="fixture-post", correlation="duplicate",
                                synced_revision=1, state="draft"))
        before = core.bridge._row_view(session.get(RemoteDraft, ident))
    remote = copy.deepcopy(records["fixture-post"])
    remote["content"] += " changed"
    monkeypatch.setattr(core.bridge, "_media", lambda *_: pytest.fail("AMBIGUOUS_MEDIA_GET_FORBIDDEN"))
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_BINDING_CHANGED"):
        core.bridge.import_post(remote, core.bridge.target("max"))
    with core.db.session() as session:
        assert session.get(Variant, ident).revision == after["revision"]
        assert core.bridge._row_view(session.get(RemoteDraft, ident)) == before


def test_fresh_post_read_is_one_uncached_unique_nonce_get(monkeypatch):
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1/public/v1")
    monkeypatch.setenv("POSTIZ_API_KEY", "fixture-only")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"posts": [{"id": "fixture-post", "state": "DRAFT"}]})
    with_client = PostizClient({}, transport=httpx.MockTransport(handle))
    try:
        assert with_client.get_post_fresh("fixture-post")["state"] == "DRAFT"
        assert with_client.get_post_fresh("fixture-post")["state"] == "DRAFT"
    finally:
        with_client.close()
    assert len(requests) == 2 and all(request.method == "GET" for request in requests)
    assert all(request.headers["Cache-Control"] == "no-cache, no-store" for request in requests)
    assert all(request.headers["Pragma"] == "no-cache" for request in requests)
    assert requests[0].url.params["owned_maxreadnonce"] != requests[1].url.params["owned_maxreadnonce"]
    assert all(request.headers["Authorization"] == "fixture-only" for request in requests)


@pytest.mark.parametrize("response", [{"posts": []}, {"posts": [{"id": "x"}, {"id": "x"}]}])
def test_fresh_post_read_never_accepts_missing_or_duplicate_result(monkeypatch, response):
    from max_postiz_agent.config import AgentError
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1/public/v1")
    monkeypatch.setenv("POSTIZ_API_KEY", "fixture-only")
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response)
    client = PostizClient({}, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AgentError, match="POSTIZ_POST_NOT_FOUND"):
            client.get_post_fresh("x")
    finally:
        client.close()
    assert len(calls) == 1



def test_fresh_integrations_use_same_no_cache_nonce_contract(monkeypatch):
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1/public/v1")
    monkeypatch.setenv("POSTIZ_API_KEY", "fixture-only")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[{"id": "fixture-integration", "identifier": "max-browser"}])
    client = PostizClient({}, transport=httpx.MockTransport(handle))
    try:
        assert client.integrations_fresh()[0]["id"] == "fixture-integration"
        assert client.integrations_fresh()[0]["identifier"] == "max-browser"
    finally:
        client.close()
    assert len(requests) == 2 and all(request.method == "GET" for request in requests)
    assert all(request.headers["Cache-Control"] == "no-cache, no-store" for request in requests)
    assert all(request.headers["Pragma"] == "no-cache" for request in requests)
    assert requests[0].url.params["owned_maxreadnonce"] != requests[1].url.params["owned_maxreadnonce"]


@pytest.mark.parametrize("state,lease", [("sending", 0), ("unknown", 0), ("scheduled", 0),
                                         ("published", 0), ("pending", 123456789)])
def test_explicit_sync_never_clears_send_outcome_or_lease(hub, monkeypatch, state, lease):
    from max_postiz_agent.config import AgentError
    core, _, after, _, _ = core_stage(hub, monkeypatch)
    ident = after["variant_id"]
    with core.db.session.begin() as session:
        approval = TargetApproval(variant_id=ident, revision=after["revision"], snapshot_hash="fixture-snapshot",
                                  scheduled_at=1, actor="fixture-only")
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=ident, approval_id=approval.id, revision=after["revision"],
                            snapshot_hash="fixture-snapshot", snapshot=after, state=state, due_at=1, lease_until=lease)
        session.add(dispatch)
        session.flush()
        dispatch_id = dispatch.id
        link_before = core.bridge._row_view(session.get(BridgePost, "fixture-post"))
    monkeypatch.setattr(core.postiz, "get_post_fresh", lambda *_: pytest.fail("NO_GET_BEFORE_SEND_RECONCILIATION"))
    with pytest.raises(AgentError, match="CANCEL_OR_RECONCILE_FIRST"):
        core.sync_remote(ident)
    with core.db.session() as session:
        assert core.bridge._row_view(session.get(BridgePost, "fixture-post")) == link_before
        assert session.get(Variant, ident).revision == after["revision"]
        assert session.get(Dispatch, dispatch_id).state == state
        assert session.get(Dispatch, dispatch_id).lease_until == lease


@pytest.mark.parametrize("case", ["unknown", "wrong_correlation", "partial_variant", "partial_correlation",
                                  "different_external_id", "retired_identity"])
def test_foreign_or_partial_native_markers_never_become_new_owner_content(hub, monkeypatch, case):
    from max_postiz_agent.config import AgentError
    from max_postiz_agent.db import Audit
    from max_postiz_agent.preapproval_retirement import _id
    core, _, after, _, records = core_stage(hub, monkeypatch)
    remote = copy.deepcopy(records["fixture-post"])
    remote["id"], remote["group"] = "untracked-foreign-post", "foreign-group"
    settings = json.loads(remote["settings"])
    if case == "unknown":
        settings["hub_variant_id"] = "unknown-variant"
    elif case == "wrong_correlation":
        settings["hub_correlation"] = "wrong-correlation"
    elif case == "partial_variant":
        del settings["hub_correlation"]
    elif case == "partial_correlation":
        del settings["hub_variant_id"]
    elif case == "retired_identity":
        with core.db.session.begin() as session:
            session.add(Audit(id=_id(after["variant_id"], "claim"), actor="fixture",
                              action="preapproval_retirement_claim", entity=after["variant_id"]))
    remote["settings"] = json.dumps(settings)
    monkeypatch.setattr(core.bridge, "_media", lambda *_: pytest.fail("FOREIGN_MARKER_MEDIA_FORBIDDEN"))
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_BINDING_CHANGED"):
        core.bridge.import_post(remote, core.bridge.target("max"))
    with core.db.session() as session:
        assert session.query(Variant).count() == session.query(BridgePost).count() == session.query(RemoteDraft).count() == 1
        assert session.get(Variant, after["variant_id"]).revision == after["revision"]
        assert session.get(BridgePost, "untracked-foreign-post") is None
        assert session.query(VariantRevision).count() == 2


def test_unmarked_manual_post_keeps_owner_content_import_path(hub, monkeypatch):
    core, _, _, _, records = core_stage(hub, monkeypatch)
    remote = copy.deepcopy(records["fixture-post"])
    remote["id"], remote["group"], remote["settings"] = "manual-post", "manual-group", json.dumps({"__type": "max-browser"})
    variant_id = core.bridge.import_post(remote, core.bridge.target("max"))
    with core.db.session() as session:
        assert session.get(Variant, variant_id).metadata_json["origin"] == "postiz_max_bridge"
        assert session.get(BridgePost, "manual-post").variant_id == variant_id
        assert session.query(RemoteDraft).count() == 1
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


@pytest.mark.parametrize("content", [None, "", "   ", "\nЗаголовок", "Заголовок\n", " Заголовок",
                                    "Заголовок ", "\u00a0Заголовок", "Заголовок\u00a0"])
@pytest.mark.parametrize("kind", ["draft", "schedule"])
def test_noncanonical_max_plaintext_fails_before_any_post(monkeypatch, content, kind):
    from max_postiz_agent.config import AgentError
    client = PostizClient({})
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: pytest.fail("NONCANONICAL_POST_FORBIDDEN"))
    target = SimpleNamespace(platform="max-browser", destination="fixture", options={})
    try:
        with pytest.raises(AgentError, match="MAX_CANONICAL_PLAINTEXT_REQUIRED"):
            client.create(target, content, [], kind=kind)
    finally:
        client.close()


@pytest.mark.parametrize("plain", ["Заголовок\n\nСтрока\u00a0с\u00a0пробелами\n\nИсточник: https://example.org/",
                                   "Заголовок\n\nA < B &quot;literal&quot;\n\nCredit: A & B. CC BY 4.0"])
def test_canonical_boundary_keeps_internal_newlines_and_entities(monkeypatch, plain):
    assert plain == plain.strip()
    test_max_transport_plaintext_is_exact_after_single_html_parse(monkeypatch, plain, "draft")


@pytest.mark.parametrize("body", ["", "   ", "\nЗаголовок", "Заголовок\n", " Заголовок", "Заголовок ", "Заголовок\u00a0"])
def test_native_stage_rejects_noncanonical_before_claim_or_upload(hub, monkeypatch, body):
    from max_postiz_agent.config import AgentError
    core, before, _, captured, records = core_stage(hub, monkeypatch, body_override=body, stage=False)
    monkeypatch.setattr(core.postiz, "upload", lambda *_args: pytest.fail("INVALID_NATIVE_UPLOAD_FORBIDDEN"))
    monkeypatch.setattr(core.postiz, "request", lambda *_args, **_kwargs: pytest.fail("INVALID_NATIVE_POST_FORBIDDEN"))
    with pytest.raises(AgentError, match="MAX_CANONICAL_PLAINTEXT_REQUIRED"):
        core.stage_remote(before["variant_id"])
    with core.db.session() as session:
        variant = session.get(Variant, before["variant_id"])
        assert core.snapshot(variant, session.get(Target, variant.target_id)) == before
        assert session.query(RemoteDraft).count() == 0
        assert session.query(BridgePost).count() == 0
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == session.query(DispatchAttempt).count() == 0
        assert session.query(VariantRevision).count() == 1
    assert captured == [] and records == {}
