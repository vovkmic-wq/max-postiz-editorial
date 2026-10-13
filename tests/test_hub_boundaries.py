"""Content integrity, API validation and remote editing conflicts."""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import Dispatch, RemoteDraft, Target, Variant
from test_hub import approve, hub as hub_fixture, variants

hub = hub_fixture


def test_api_rejects_incomplete_and_non_boolean_inputs(hub):
    client = TestClient(hub_app(hub))
    headers = {"Authorization": "Bearer hub-test-owner"}
    assert client.post("/api/hub/variants/nope/approve", headers=headers, json={}).status_code == 422
    assert client.post("/api/hub/targets/max/pause", headers=headers, json={"paused": "false"}).status_code == 422
    assert client.post("/api/v1/images/schedule/resume", headers=headers).status_code == 404


def test_media_validated_and_frozen(hub):
    ident = variants(hub)["pikabu"]
    client = TestClient(hub_app(hub))
    headers = {"Authorization": "Bearer hub-test-owner"}
    assert client.post("/api/hub/media", headers=headers, files={"file": ("bad.png", b"garbage", "image/png")}).status_code == 422
    image = io.BytesIO()
    Image.new("RGB", (24, 24), "blue").save(image, format="PNG")
    response = client.post("/api/hub/media", headers=headers, files={"file": ("test.png", image.getvalue(), "image/png")})
    assert response.status_code == 200
    media = response.json()
    with hub.db.session() as s:
        v = s.get(Variant, ident)
    result = hub.revise_variant(ident, 1, v.body, media=[media], metadata_payload={"tags": ["Видео"], "community": ""})
    assert "Контент нейросетей" in result["metadata_json"]["tags"]
    dispatch = approve(hub, ident)
    (hub.settings.root / media["path"]).write_bytes(b"changed")
    hub.publish_due()
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatch).error == "MEDIA_HASH_CHANGED"


def test_remote_revision_and_local_edit_conflict(hub, monkeypatch):
    ident = variants(hub)["max"]
    with hub.db.session.begin() as s:
        t = s.get(Target, "max")
        t.route = "postiz"
    monkeypatch.setattr(hub.postiz, "create", lambda *a: "remote-draft")
    assert hub.stage_remote(ident) == "remote-draft"
    with hub.db.session() as s:
        original = s.get(Variant, ident).body
    monkeypatch.setattr(hub.postiz, "get_post", lambda *a: {"state": "DRAFT", "content": original + "\nПравка редактора."})
    result = hub.sync_remote(ident)
    assert result["revision"] == 2
    assert hub.sync_remote(ident) == {"state": "unchanged"}
    hub.revise_variant(ident, 2, original + "\nЛокальная правка.")
    monkeypatch.setattr(hub.postiz, "get_post", lambda *a: {"state": "DRAFT", "content": "Другая правка"})
    with pytest.raises(AgentError, match="REMOTE_EDIT_CONFLICT"):
        hub.sync_remote(ident)
    with hub.db.session() as s:
        assert s.get(RemoteDraft, ident).synced_revision == 2


def test_failed_remote_cancel_becomes_unknown(hub, monkeypatch):
    dispatch = approve(hub, variants(hub)["max"])
    with hub.db.session.begin() as s:
        d = s.get(Dispatch, dispatch)
        d.state, d.external_id = "scheduled", "external"
    def uncertain(*args):
        raise AgentError("POSTIZ_UNKNOWN")
    monkeypatch.setattr(hub.postiz, "delete", uncertain)
    with pytest.raises(AgentError, match="REMOTE_CANCEL_UNCONFIRMED"):
        hub.cancel_dispatch(dispatch)
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatch).state == "unknown"


def test_unauthorized_source_is_not_archived(hub):
    source = hub.settings.source("example_rss")
    source["rights"]["allow_storage"] = False
    with pytest.raises(AgentError, match="SOURCE_STORAGE_NOT_AUTHORIZED"):
        hub.collect("example_rss")
    assert not (hub.settings.root / "data/archive/example_rss").exists()


def test_offline_adapt_fails_explicitly(hub):
    with pytest.raises(AgentError, match="OFFLINE_ADAPTATION_UNAVAILABLE"):
        hub.adapt_variant(variants(hub)["max"])


def test_encrypted_backup_includes_archive_and_pauses_restored_targets(hub):
    from cryptography.fernet import Fernet
    from max_postiz_agent.backup import backup, restore
    from max_postiz_agent.db import Channel, Database
    from max_postiz_agent.hub import HubService
    variants(hub)
    blob = hub.settings.root / "data/archive/example_rss/fixture.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"archived source")
    with hub.db.session.begin() as s:
        for t in s.query(Target):
            t.paused = True
        for c in s.query(Channel):
            c.paused = True
    key = Fernet.generate_key()
    archive = hub.settings.root / "snapshot.enc"
    backup(hub, archive, key)
    new_db = Database(hub.settings, url="sqlite:///" + (hub.settings.root / "restored.db").as_posix())
    new_db.initialize()
    restored = HubService(hub.settings, new_db, bootstrap_targets=False)
    try:
        restore(restored, archive, key)
        with new_db.session() as s:
            assert len(list(s.query(Variant))) == 2
            assert all(t.paused for t in s.query(Target))
        assert blob.read_bytes() == b"archived source"
    finally:
        restored.close()


def test_history_import_dry_run_idempotent_and_archived(hub):
    import sqlite3
    from max_postiz_agent.db import Database, Draft
    from max_postiz_agent.history_import import import_history
    variants(hub)
    source = hub.settings.root / "source-copy.db"
    with sqlite3.connect(hub.db.engine.url.database) as reader, sqlite3.connect(source) as writer:
        reader.backup(writer)
    target = Database(hub.settings, url="sqlite:///" + (hub.settings.root / "imported.db").as_posix())
    target.initialize()
    try:
        proposed = import_history(target, source)
        assert proposed["new_rows"]["drafts"] == 1
        with target.session() as s:
            assert s.query(Draft).count() == 0
        imported = import_history(target, source, apply=True)
        assert imported["active_publications_imported"] == 0
        with target.session() as s:
            assert s.query(Draft).one().state == "archived"
        again = import_history(target, source, apply=True)
        assert all(n == 0 for n in again["new_rows"].values())
    finally:
        target.engine.dispose()


def test_standalone_archived_story_searchable_and_not_reactivated(hub):
    from max_postiz_agent.db import Draft, Story
    ident = variants(hub)["max"]
    with hub.db.session.begin() as s:
        v = s.get(Variant, ident)
        d = s.get(Draft, v.draft_id)
        d.state, d.config_id = "archived", "old-configuration"
        s.add(Story(text_hash="standalone-test", title="Собственный архив", text="Сохранённая редакционная история"))
    # Source uses browser mode as a reason to stale old drafts; archives must remain archived.
    original_mode = hub.publisher.mode
    hub.publisher.mode = "browser"
    try:
        hub.activate()
    finally:
        hub.publisher.mode = original_mode
    with hub.db.session() as s:
        assert s.get(Draft, v.draft_id).state == "archived"
    assert hub.search_archive("редакционная")[0]["kind"] == "standalone_story"


def test_portable_archive_excludes_nested_environment(hub):
    import zipfile
    from max_postiz_agent.portability import package_project
    private = hub.settings.root / "deployment/postiz/.env"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("PRIVATE=fixture-private-value", encoding="utf-8")
    destination = hub.settings.root / "project.zip"
    package_project(hub.settings.root, destination)
    with zipfile.ZipFile(destination) as archive:
        assert "deployment/postiz/.env" not in archive.namelist()


def test_binding_reload_blocks_previous_approval(hub):
    import json
    dispatch = approve(hub, variants(hub)["max"])
    path = hub.settings.root / "config/publishing.json"
    publishing = json.loads(path.read_text(encoding="utf-8"))
    publishing["targets"][0]["destination"] = "-456"
    path.write_text(json.dumps(publishing), encoding="utf-8")
    hub.activate()
    with hub.db.session() as s:
        assert s.get(Target, "max").paused
    hub.pause_target("max", False)
    assert hub.publish_due() == 0
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatch).state == "cancelled"


def test_recurring_task_resume_digest_and_config_removal(hub):
    import json
    from max_postiz_agent.db import Draft, now
    from max_postiz_agent.hub_models import RecurringTask
    variants(hub)
    path = hub.settings.root / "config/publishing.json"
    publishing = json.loads(path.read_text(encoding="utf-8"))
    publishing["tasks"] = [{"id": "digest", "profile_id": "main", "interval_seconds": 3600,
                            "lookback_hours": 24, "enabled": True}]
    path.write_text(json.dumps(publishing), encoding="utf-8")
    hub.activate()
    with hub.db.session.begin() as s:
        task = s.get(RecurringTask, "digest")
        task.attempts, task.error, task.due_at = 3, "PROVIDER_UNAVAILABLE", now() - 1
    hub.recurring_due()
    with hub.db.session() as s:
        assert s.query(Draft).count() == 1
    hub.resume_task("digest")
    hub.recurring_due()
    hub.recurring_due()
    with hub.db.session() as s:
        assert s.query(Draft).count() == 2
        assert s.get(RecurringTask, "digest").error is None
    publishing["tasks"] = []
    path.write_text(json.dumps(publishing), encoding="utf-8")
    hub.activate()
    with hub.db.session() as s:
        assert not s.get(RecurringTask, "digest").enabled
