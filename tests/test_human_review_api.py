"""Panel human attestation is unavailable to API tokens or unchecked sessions."""
import pytest
from fastapi.testclient import TestClient

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import now
from max_postiz_agent.hub_api import hub_app
from test_hub import hub as hub_fixture, variants

hub = hub_fixture
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def review_api(hub, monkeypatch):
    monkeypatch.setenv("APP_EDITOR_SECRET", "hub-test-editor")
    ident = variants(hub)["max"]
    calls = []
    details = {"hourly_article": True, "human_review_required": True,
               "source_title": "Independent source", "source_body": "English original.",
               "source_url": "https://example.org/source", "machine_review_current": True,
               "status": "awaiting_human_review", "revision": 1, "content_hash": "a" * 64}
    monkeypatch.setattr(hub, "hourly_review_details", lambda requested: details)

    def approve(*args, **kwargs):
        calls.append((args, kwargs))
        return "one-approved-dispatch"

    monkeypatch.setattr(hub, "approve_variant", approve)
    client = TestClient(hub_app(hub))
    body = {"revision": 1, "content_hash": "a" * 64, "due_at": now() + 3600,
            "human_review_confirmed": True}
    return hub, client, ident, body, calls, details


def login(client, role="owner"):
    response = client.post("/api/v1/session/login",
                           headers={"Authorization": "Bearer hub-test-" + role})
    assert response.status_code == 200


def test_source_is_authenticated_and_viewer_readable(review_api):
    _, client, ident, _, _, details = review_api
    path = f"/api/hub/variants/{ident}/article-review"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={"Authorization": "Bearer hub-test-viewer"})
    assert response.status_code == 200 and response.json() == details


@pytest.mark.parametrize("cookie", [None, "unsigned-editor-session"])
def test_checked_body_without_a_valid_signed_session_cannot_attest(review_api, cookie):
    _, client, ident, body, calls, _ = review_api
    if cookie:
        client.cookies.set("max_panel_session", cookie)
    response = client.post(f"/api/hub/variants/{ident}/approve", headers=ORIGIN, json=body)
    assert response.status_code == 401 and not calls


def test_unavailable_hourly_details_cannot_fall_back_to_general_approval(review_api, monkeypatch):
    hub, client, ident, body, calls, _ = review_api

    def unavailable(_):
        raise AgentError("ARTICLE_SOURCE_CHANGED", 409)

    monkeypatch.setattr(hub, "hourly_review_details", unavailable)
    login(client)
    response = client.post(f"/api/hub/variants/{ident}/approve", headers=ORIGIN, json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "ARTICLE_SOURCE_CHANGED" and not calls


@pytest.mark.parametrize("confirmed", [False, True])
def test_bearer_cannot_attest_human_even_with_cookie(review_api, confirmed):
    _, client, ident, body, calls, _ = review_api
    login(client)
    response = client.post(f"/api/hub/variants/{ident}/approve",
        headers={**ORIGIN, "Authorization": "Bearer hub-test-owner"},
        json={**body, "human_review_confirmed": confirmed})
    assert response.status_code == 403
    assert response.json()["code"] == "HUMAN_REVIEW_SESSION_REQUIRED"
    assert not calls


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_only_checked_same_origin_owner_or_editor_session_attests(review_api, role):
    _, client, ident, body, calls, _ = review_api
    login(client, role)
    response = client.post(f"/api/hub/variants/{ident}/approve", headers=ORIGIN, json=body)
    assert response.status_code == 200
    assert calls == [((ident, body["revision"], body["content_hash"], body["due_at"], role),
                      {"human_confirmed": True})]


@pytest.mark.parametrize("field", [{}, {"human_review_confirmed": False}])
def test_unchecked_checkbox_never_calls_approval(review_api, field):
    _, client, ident, body, calls, _ = review_api
    login(client)
    body.pop("human_review_confirmed")
    response = client.post(f"/api/hub/variants/{ident}/approve", headers=ORIGIN,
                           json={**body, **field})
    assert response.status_code == 409
    assert response.json()["code"] == "HUMAN_REVIEW_CONFIRMATION_REQUIRED"
    assert not calls


@pytest.mark.parametrize("value", ["true", 1, None])
def test_confirmation_is_a_strict_boolean(review_api, value):
    _, client, ident, body, calls, _ = review_api
    login(client)
    response = client.post(f"/api/hub/variants/{ident}/approve", headers=ORIGIN,
                           json={**body, "human_review_confirmed": value})
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("origin", [None, "https://other.example"])
def test_human_review_requires_same_origin(review_api, origin):
    _, client, ident, body, calls, _ = review_api
    login(client)
    response = client.post(f"/api/hub/variants/{ident}/approve",
        headers={} if origin is None else {"Origin": origin}, json=body)
    assert response.status_code == 403 and not calls


def test_viewer_cookie_cannot_approve_or_recheck(review_api, monkeypatch):
    hub, client, ident, body, calls, _ = review_api
    rechecks = []
    monkeypatch.setattr(hub, "recheck_hourly_variant", lambda *args: rechecks.append(args))
    login(client, "viewer")
    for action, payload in [("approve", body), ("recheck-article", {
            "revision": body["revision"], "content_hash": body["content_hash"]})]:
        assert client.post(f"/api/hub/variants/{ident}/{action}", headers=ORIGIN,
                           json=payload).status_code == 403
    assert not calls and not rechecks


def test_recheck_forwards_exact_version_without_attestation(review_api, monkeypatch):
    hub, client, ident, body, approvals, details = review_api
    checks = []

    def recheck(*args):
        checks.append(args)
        return {**details, "revision": 2, "content_hash": "b" * 64}

    monkeypatch.setattr(hub, "recheck_hourly_variant", recheck)
    payload = {"revision": body["revision"], "content_hash": body["content_hash"]}
    response = client.post(f"/api/hub/variants/{ident}/recheck-article",
                           headers={"Authorization": "Bearer hub-test-editor"}, json=payload)
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert checks == [(ident, 1, "a" * 64)] and not approvals


@pytest.mark.parametrize("code", ["REVISION_CONFLICT", "ARTICLE_MACHINE_RECHECK_REQUIRED"])
def test_human_checkbox_cannot_override_core_version_or_machine_proof(review_api, monkeypatch, code):
    hub, client, ident, body, calls, _ = review_api

    def blocked(*args, **kwargs):
        raise AgentError(code, 409)

    monkeypatch.setattr(hub, "approve_variant", blocked)
    login(client)
    response = client.post(f"/api/hub/variants/{ident}/approve", headers=ORIGIN, json=body)
    assert response.status_code == 409 and response.json()["code"] == code
    assert not calls


def test_general_route_keeps_api_approval_without_human_attestation(review_api, monkeypatch):
    hub, client, ident, body, calls, _ = review_api
    monkeypatch.setattr(hub, "hourly_review_details", lambda requested: {"hourly_article": False})
    response = client.post(f"/api/hub/variants/{ident}/approve",
        headers={"Authorization": "Bearer hub-test-owner"}, json=body)
    assert response.status_code == 200
    assert calls[0][1] == {"human_confirmed": False}
