"""Проверки переноса истории, конкурентных изменений и общей паузы."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.server_api import editorial_app
from max_postiz_agent.server_models import EditorialLedger
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState, default_settings

pytest_plugins = ["test_article_cycle"]


def test_migration_preserves_source_history_and_legacy_file(cycle):
    old, core, article, _, _, _, _ = cycle
    prepared = old.prepare(article)
    before = old.ledger_path.read_bytes()
    payload = json.loads(before)
    proof = DatabaseArticleLedger(old).migrate()
    assert proof["records"] == 1 and proof["hash"] == fingerprint(payload)
    assert old.ledger_path.read_bytes() == before
    fresh = HourlyArticleCycle(core)
    assert fresh._load() == payload
    assert fresh._load()["records"][0]["id"] == prepared["id"]
    changed = fresh._load()
    changed["records"][0]["state"] = "cancelled"
    fresh._save(changed)
    assert fresh.ledger_path.read_bytes() == before
    assert HourlyArticleCycle(core)._load()["records"][0]["state"] == "cancelled"


def test_stale_ledger_writer_cannot_overwrite_history(cycle):
    old, core, *_ = cycle
    DatabaseArticleLedger(old).migrate()
    first, second = HourlyArticleCycle(core), HourlyArticleCycle(core)
    a, b = first._load(), second._load()
    a["records"].append({"id": "one", "slot": 1, "state": "cancelled"})
    b["records"].append({"id": "two", "slot": 2, "state": "cancelled"})
    first._save(a)
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_REVISION_CONFLICT"):
        second._save(b)
    assert HourlyArticleCycle(core)._load() == a


def test_missing_database_never_falls_back_to_old_json(cycle):
    old, core, *_ = cycle
    DatabaseArticleLedger(old).migrate()
    with core.db.session.begin() as session:
        session.execute(delete(EditorialLedger))
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_DATABASE_MISSING"):
        HourlyArticleCycle(core)._load()


def test_common_pause_blocks_even_direct_legacy_cli(cycle):
    old, core, article, _, calls, *_ = cycle
    store = ServerState(core)
    store.initialize()
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        old.prepare(article)
    assert calls == [] and old._load() == {"records": []}
    assert core.publish_due() == 0


def test_lost_leader_fences_the_next_mutation(cycle):
    old, core, article, _, calls, *_ = cycle
    def lost():
        raise AgentError("EDITORIAL_LEADERSHIP_LOST", 409)
    core.editorial_leader_check = lost
    with pytest.raises(AgentError, match="EDITORIAL_LEADERSHIP_LOST"):
        old.prepare(article)
    assert calls == [] and old._load() == {"records": []}


def test_settings_revision_is_independent_from_worker_heartbeat(cycle):
    _, core, *_ = cycle
    store = ServerState(core)
    first = store.initialize()
    store.phase("translating", record_id="record")
    assert store.read()["revision"] == first["revision"]
    settings = default_settings()
    settings["nasa"]["keywords"] = ["ocean"]
    second = store.settings(settings, first["revision"], "postiz:owner")
    assert second["revision"] == first["revision"] + 1
    with pytest.raises(AgentError, match="EDITORIAL_REVISION_CONFLICT"):
        store.settings(default_settings(), first["revision"], "postiz:owner")
    store.control("start", second["revision"], "postiz:owner")
    with pytest.raises(AgentError, match="EDITORIAL_PAUSE_BEFORE_SETTINGS"):
        store.settings(settings, store.read()["revision"], "postiz:owner")


@pytest.fixture
def api(cycle, monkeypatch):
    _, core, *_ = cycle
    store = ServerState(core)
    store.initialize()
    monkeypatch.setenv("EDITORIAL_INTERNAL_KEY", "a" * 48)
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "owner-id")
    monkeypatch.setenv("EDITORIAL_OWNER_ORGANIZATION_ID", "org-id")
    headers = {"X-Editorial-Key": "a" * 48, "X-Editorial-Owner": "owner-id",
               "X-Editorial-Organization": "org-id"}
    return TestClient(editorial_app(core, store)), store, headers


def test_postiz_owner_identity_and_machine_key_are_both_required(api):
    client, _, headers = api
    assert client.get("/internal/editorial/state").status_code == 403
    assert client.get("/internal/editorial/state", headers={**headers,
        "X-Editorial-Owner": "other"}).status_code == 403
    response = client.get("/internal/editorial/state", headers=headers)
    assert response.status_code == 200 and response.json()["paused"] is True
    assert response.json()["capabilities"]["reddit"]["available"] is False
    assert "a" * 48 not in response.text
    assert client.get("/hub").status_code == 404
    assert client.post("/api/hub/variants/example/approve").status_code == 404


def test_postiz_settings_preserve_structured_filters_and_reject_stale_forms(api):
    client, store, headers = api
    value = copy.deepcopy(client.get("/internal/editorial/settings", headers=headers).json())
    value["settings"]["reddit"].update(authors=["scientist"], keywords=["ocean"], min_score=100)
    response = client.put("/internal/editorial/settings", headers=headers, json=value)
    assert response.status_code == 200
    assert store.read()["settings"]["reddit"]["min_score"] == 100
    assert client.put("/internal/editorial/settings", headers=headers, json=value).status_code == 409
    value = response.json()
    value["settings"]["source"] = "reddit"
    assert client.put("/internal/editorial/settings", headers=headers, json=value).status_code == 409


def test_input_validation_does_not_echo_private_values(api):
    client, _, headers = api
    response = client.post("/internal/editorial/control", headers=headers,
        json={"action": "start", "revision": "private-input"})
    assert response.status_code == 422 and "private-input" not in response.text
    assert client.post("/internal/editorial/control", headers=headers,
        json={"action": "pause", "revision": True}).status_code == 422
