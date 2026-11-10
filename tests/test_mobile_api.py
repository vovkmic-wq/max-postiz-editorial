"""Mobile authorization, durable command claims and network contract regressions."""
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.db import Database, now
from max_postiz_agent.mobile_api import mobile_app
from max_postiz_agent.mobile_auth import MobileOIDCAuth, MobileOIDCConfig, MobilePrincipal
from max_postiz_agent.mobile_store import MobileJournal, PLAN_TTL
from test_hub_postgres import pg_hub  # noqa: F401


class Auth:
    def authenticate(self, authorization):
        roles = {"owner": "owner", "operator": "operator", "viewer": "viewer", "stale": "owner"}
        if authorization.removeprefix("Bearer ") not in roles:
            raise AgentError("MOBILE_AUTH_REQUIRED", 401)
        subject = authorization.removeprefix("Bearer ")
        auth_time = now() - 400 if subject == "stale" else now()
        return MobilePrincipal("fixture:" + subject, roles[subject], auth_time)


class Domain:
    def __init__(self):
        self.executions = []
        self.entered = None
        self.release = None
        self.fail = False

    def read(self, resource, *, object_id=None, filters):
        return {"available_actions": ["hold_item", "resume_project"], "resource": resource}

    def plan(self, action, object_id):
        return {"effect": action, "secret_binding": "private", "public": {"label": object_id}}

    def execute(self, action, object_id, plan, *, actor, operation_id, reason):
        self.executions.append(operation_id)
        if self.entered:
            self.entered.set()
            assert self.release.wait(10)
        if self.fail:
            raise RuntimeError("private transport failure")
        return {"external_mutations": 0}


def headers(subject):
    return {"Authorization": f"Bearer {subject}"}


def plan(client, action="hold_item", subject="operator"):
    response = client.post("/mobile/v1/operation-plans", headers=headers(subject),
                           json={"action": action, "object_id": "fixture"})
    assert response.status_code == 201
    assert "secret_binding" not in response.text
    return response.json()


def body(value):
    return {"operation_id": str(uuid4()), "plan_id": value["plan_id"],
            "plan_hash": value["plan_hash"], "confirmed": True, "reason": "fixture decision"}


@pytest.fixture
def mobile(project):
    db = Database(Settings.load(project))
    db.initialize()
    domain = Domain()
    with TestClient(mobile_app(db, domain, Auth(), environment="test", write_enabled=True)) as client:
        yield client, domain
    db.engine.dispose()


def test_role_scope_and_step_up(mobile):
    client, _ = mobile
    assert client.get("/mobile/v1/overview").status_code == 401
    response = client.get("/mobile/v1/overview", headers=headers("viewer"))
    assert response.json()["available_actions"] == []
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/mobile/v1/overview", headers=headers("operator")).json()[
        "available_actions"] == ["hold_item"]
    for subject, code in (("viewer", "MOBILE_ACTION_FORBIDDEN"), ("stale", "MOBILE_REAUTH_REQUIRED")):
        response = client.post("/mobile/v1/operation-plans", headers=headers(subject),
                               json={"action": "retire_preapproval_no_send", "object_id": "fixture"})
        assert response.status_code == 403
        assert response.json()["code"] == code


def test_replay_returns_same_receipt_and_rejects_changed_payload(mobile):
    client, domain = mobile
    value = plan(client)
    request = body(value)
    def send(value, subject="operator"):
        return client.post("/mobile/v1/operations", headers=headers(subject), json=value)
    result = send(request)
    assert result.status_code == 202
    assert result.json()["state"] == "succeeded"
    assert send(request).json() == result.json()
    assert send({**request, "reason": "modified reason"}).status_code == 409
    assert send({**request, "operation_id": str(uuid4())}).status_code == 409
    assert send(request, subject="owner").status_code == 404
    assert len(domain.executions) == 1
    path = f"/mobile/v1/operations/{request['operation_id']}"
    assert client.get(path, headers=headers("operator")).json() == result.json()
    assert client.get(path, headers=headers("owner")).status_code == 404


def test_unexpected_result_is_durable_unknown_and_never_reexecuted(mobile):
    client, domain = mobile
    domain.fail = True
    request = body(plan(client))
    for _ in range(2):
        response = client.post("/mobile/v1/operations", headers=headers("operator"), json=request)
        assert response.json()["state"] == "unknown"
        assert "private transport failure" not in response.text
    assert len(domain.executions) == 1


def test_expired_unaccepted_operation_requires_exact_plan_and_actor(mobile, monkeypatch):
    client, domain = mobile
    request = body(plan(client))
    used = body(plan(client))
    sent = client.post("/mobile/v1/operations", headers=headers("operator"), json=used)
    assert sent.status_code == 202
    path = f"/mobile/v1/operations/{request['operation_id']}"
    query = {key: request[key] for key in ("plan_id", "plan_hash")}
    assert client.get(path, params=query, headers=headers("operator")).status_code == 404
    monkeypatch.setattr("max_postiz_agent.mobile_store.now", lambda: now() + PLAN_TTL + 1)
    assert client.get(path, headers=headers("operator")).status_code == 404
    assert client.get(path, params={"plan_id": query["plan_id"]}, headers=headers("operator")).status_code == 422
    assert client.get(path, params={**query, "plan_hash": "f" * 64}, headers=headers("operator")).status_code == 404
    assert client.get(path, params=query, headers=headers("owner")).status_code == 404
    assert client.get(path, params={key: used[key] for key in query}, headers=headers("operator")).status_code == 404
    result = client.get(path, params=query, headers=headers("operator"))
    assert result.status_code == 410 and result.json()["code"] == "MOBILE_OPERATION_NOT_ACCEPTED"
    assert result.headers["Cache-Control"] == "no-store"
    assert domain.executions == [used["operation_id"]]


def test_postgres_expiry_status_waits_for_in_flight_claim(pg_hub, monkeypatch):  # noqa: F811
    core, _ = pg_hub
    domain = Domain()
    journal = MobileJournal(core.db, "test")
    entered, release = Event(), Event()
    with TestClient(mobile_app(core.db, domain, Auth(), environment="test", write_enabled=True)) as client:
        request = body(plan(client))
        path = f"/mobile/v1/operations/{request['operation_id']}"
        query = {key: request[key] for key in ("plan_id", "plan_hash")}

        def before_commit(_action):
            entered.set()
            assert release.wait(8)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(journal.claim, "fixture:operator", request["operation_id"], request["plan_id"],
                                request["plan_hash"], request["reason"], before_commit)
            try:
                assert entered.wait(5)
                assert client.get(path, headers=headers("operator")).status_code == 404
                monkeypatch.setattr("max_postiz_agent.mobile_store.now", lambda: now() + PLAN_TTL + 1)
                receipt = pool.submit(client.get, path, params=query, headers=headers("operator"))
                with pytest.raises(TimeoutError):
                    receipt.result(.3)
            finally:
                release.set()
            assert first.result(5)[1]["operation_id"] == request["operation_id"]
            result = receipt.result(5)
            assert result.status_code == 200 and result.json()["state"] == "running"
    assert domain.executions == []


def test_invalid_payloads_are_bounded_and_redacted(mobile):
    client, _ = mobile
    url = "/mobile/v1/operation-plans"
    response = client.post(url, headers=headers("operator"),
                           json={"action": "SECRET_SENTINEL", "object_id": "fixture"})
    assert response.status_code == 422
    assert "SECRET_SENTINEL" not in response.text
    response = client.post(url, headers=headers("operator"), json={"extra": "x" * 20000})
    assert response.status_code == 413
    assert client.get("/mobile/v1/queue?state=x&state=y", headers=headers("operator")).status_code == 422


def test_postgres_concurrent_mobile_confirmations_execute_once(pg_hub):  # noqa: F811
    core, _ = pg_hub
    domain = Domain()
    domain.entered, domain.release = Event(), Event()
    with TestClient(mobile_app(core.db, domain, Auth(), environment="test", write_enabled=True)) as client:
        request = body(plan(client))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, "/mobile/v1/operations", headers=headers("operator"),
                                json=request)
            assert domain.entered.wait(10)
            try:
                duplicate = client.post("/mobile/v1/operations", headers=headers("operator"),
                                        json=request)
                assert duplicate.json()["state"] == "running"
                other_id = client.post("/mobile/v1/operations", headers=headers("operator"),
                                       json={**request, "operation_id": str(uuid4())})
                assert other_id.status_code == 409
            finally:
                domain.release.set()
            assert first.result(10).json()["state"] == "succeeded"
    assert len(domain.executions) == 1


def test_oidc_introspection_checks_audience_client_and_revocation():
    config = MobileOIDCConfig("https://idp.invalid", "https://idp.invalid/introspection",
                              "service", "fixture-only", "android", "gateway", {"user": "owner"})
    claims = {"active": True, "iss": config.issuer, "aud": ["gateway"], "client_id": "android",
              "sub": "user", "exp": now() + 120, "auth_time": now()}
    def respond(request):
        assert request.method == "POST"
        assert request.url == config.endpoint
        assert "token=access-token" in request.content.decode()
        return httpx.Response(200, content=json.dumps(claims))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        auth = MobileOIDCAuth(config, client=client)
        actor = auth.authenticate("Bearer access-token")
        actor.authorize("resume_project", True, fresh=True)
        claims["aud"] = ["different"]
        with pytest.raises(AgentError, match="MOBILE_SESSION_INVALID"):
            auth.authenticate("Bearer access-token")
        claims["active"] = False
        with pytest.raises(AgentError, match="MOBILE_SESSION_EXPIRED"):
            auth.authenticate("Bearer access-token")
