"""Automatic cards describe evidence and scheduling without human-attestation controls."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import Dispatch, TargetApproval, Variant

pytest_plugins = ["test_automatic_policy"]
VIEWER = {"Authorization": "Bearer hub-test-viewer"}


def test_automatic_details_are_read_only_flags_with_full_warnings_and_evidence(automatic_cycle):
    (cycle, core, article, _, _, _, _), review = automatic_cycle
    prepared = cycle.prepare(article)
    client = TestClient(hub_app(core, dispatch_only=True))
    response = client.get(f"/api/hub/variants/{prepared['variant_id']}/article-review", headers=VIEWER)
    assert response.status_code == 200
    details = response.json()
    assert details["automatic_policy"] is True and details["human_review_required"] is False
    assert details["automatic_machine_review_valid"] is True
    assert details["machine_exception_eligible"] is False
    assert details["status"] == "automatic_ready" and details["target_paused"] is True
    assert details["dispatch_state"] is None and details["delivery_verified"] is False
    assert details["machine_review_warnings"] == review["warnings"]
    assert details["machine_review_evidence"]["review"] == review
    assert details["review_rubric"] == "meaning-first-v1"
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_automatic_details_do_not_call_a_forged_machine_proof_ready(automatic_cycle):
    (cycle, core, article, *_), _ = automatic_cycle
    prepared = cycle.prepare(article)
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        variant.metadata_json = {**variant.metadata_json, "machine_review_evidence_hash": "0" * 64}
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["automatic_policy"] is True and details["human_review_required"] is False
    assert details["automatic_machine_review_valid"] is False
    assert details["status"] == "automatic_blocked" and details["machine_exception_eligible"] is False


def test_unverified_published_state_is_not_labelled_verified_delivery(automatic_cycle):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    with core.db.session.begin() as session:
        approval = TargetApproval(variant_id=prepared["variant_id"], revision=prepared["revision"],
                                  snapshot_hash="0" * 64, scheduled_at=stamp[0], actor="fixture-only")
        session.add(approval)
        session.flush()
        session.add(Dispatch(variant_id=prepared["variant_id"], approval_id=approval.id,
                             revision=prepared["revision"], snapshot_hash="0" * 64,
                             snapshot={}, due_at=stamp[0], state="published", verified=False))
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["dispatch_state"] == "published"
    assert details["status"] == "published_unverified" and details["delivery_verified"] is False


def test_persisted_human_draft_is_not_relabelled_automatic_when_global_policy_changes(human_cycle):
    cycle, core, article, _, _, _, policy = human_cycle
    prepared = cycle.prepare(article)
    policy["approval_mode"] = "automatic"
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["human_review_required"] is True and details["automatic_policy"] is False
    assert details["status"] == "awaiting_human_review"
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_manual_approval_endpoint_cannot_approve_automatic_cards_even_in_synthetic_session(automatic_cycle, monkeypatch):
    (cycle, core, article, *_), _ = automatic_cycle
    prepared = cycle.prepare(article)
    monkeypatch.setattr(core, "approve_variant", lambda *args, **kwargs: pytest.fail("No manual approval"))
    client = TestClient(hub_app(core))
    response = client.post(f"/api/hub/variants/{prepared['variant_id']}/approve",
        headers={"Authorization": "Bearer hub-test-owner"},
        json={"revision": prepared["revision"], "content_hash": prepared["content_hash"],
              "due_at": 2000000000, "human_review_confirmed": True})
    assert response.status_code == 409
    assert response.json()["code"] == "ARTICLE_AUTOMATIC_POLICY_REQUIRED"


def test_dispatch_only_worker_label_does_not_claim_human_or_automatic_delivery(automatic_cycle):
    (_, core, *_), _ = automatic_cycle
    app = hub_app(core, run_worker=False, dispatch_only=True)
    assert app.state.worker_mode == "hourly_dispatch_only"
    response = TestClient(app).get("/api/hub/state", headers=VIEWER)
    assert response.status_code == 200
    assert response.json()["worker_mode"] == "hourly_dispatch_only"
    assert response.json()["worker_enabled"] is False


def test_synthetic_automatic_dom_hides_consent_forms_and_keeps_legacy_human_controls():
    playwright = pytest.importorskip("playwright.sync_api")
    assets = Path(__file__).resolve().parents[1] / "src/max_postiz_agent/hub_static"
    base = {"id": "auto-fixture", "target_id": "max", "title": "Технический черновик",
            "body": "Русский текст для проверки интерфейса.", "revision": 1, "content_hash": "a" * 64,
            "state": "review_pending", "media": [], "sources": [{"url": "https://example.org/source"}],
            "metadata_json": {"origin": "hourly_article_test", "approval_mode": "automatic",
                "review_rubric": "meaning-first-v1", "postiz_post_id": "fixture-only",
                "postiz_state": "QUEUE", "postiz_due_at": 2000000000}}
    human = copy.deepcopy(base)
    human["id"], human["metadata_json"]["approval_mode"] = "human-fixture", "human"
    details = {"hourly_article": True, "automatic_policy": True, "human_review_required": False,
               "automatic_machine_review_valid": True, "source_title": "An English original",
               "source_body": "The source contains <script>untrusted()</script> text.",
               "source_url": "https://example.org/source", "machine_review_current": True,
               "machine_review_status": "passed", "machine_review_issues": [], "target_paused": True,
               "machine_review_warnings": ["Optional wording <script>untrusted()</script>."],
               "review_rubric": "meaning-first-v1", "status": "pending",
               "machine_review_evidence": {"review_raw": "fixture raw", "review": {"warnings": ["Advice"]}}}
    human_details = {**details, "automatic_policy": False, "human_review_required": True,
                     "status": "awaiting_human_review", "machine_exception_eligible": False}
    state = {"database": "fixture", "model": "fixture", "model_mode": "offline",
             "postiz_configured": True, "postiz_public_origin": "http://offline.test",
             "role": "owner", "tasks": [], "dispatches": [], "variants": [base, human],
             "targets": [{"id": "max", "platform": "max", "route": "max_browser", "enabled": True,
                          "paused": True, "destination": "fixture-only", "options": {}}]}
    requests, errors = [], []

    def route(handler):
        request = handler.request
        requests.append((request.method, request.url))
        assert request.method == "GET" and request.url.startswith("http://offline.test/")
        path = request.url.removeprefix("http://offline.test")
        if path == "/hub":
            return handler.fulfill(body=(assets / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if path.startswith("/hub-assets/"):
            name = path.rsplit("/", 1)[1]
            return handler.fulfill(body=(assets / name).read_text(encoding="utf-8"),
                content_type="text/javascript" if name.endswith(".js") else "text/css")
        if path == "/api/hub/state":
            return handler.fulfill(json=state)
        if path == "/api/hub/reddit":
            return handler.fulfill(json={"items": [], "mail": {"state": "disabled", "enabled": False},
                                          "options": {"processing_enabled": False}})
        if path.endswith("/article-review"):
            return handler.fulfill(json=human_details if "human-fixture" in path else details)
        raise AssertionError("Unexpected request in offline DOM test")

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", route)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://offline.test/hub")
            auto, old_human = page.locator("#variants > article").nth(0), page.locator("#variants > article").nth(1)
            auto.get_by_label("Автоматический маршрут", exact=True).wait_for()
            assert auto.get_by_role("checkbox").count() == 0
            assert auto.get_by_role("button").count() == 0
            assert auto.locator(".human-review,.machine-exception").count() == 0
            assert auto.get_by_label("Заголовок", exact=True).get_attribute("readonly") is not None
            assert auto.get_by_label("Текст публикации", exact=True).get_attribute("readonly") is not None
            assert auto.get_by_label("Неблокирующие замечания", exact=True).inner_text() == details["machine_review_warnings"][0]
            assert auto.locator("script").count() == 0
            assert "само по себе не подтверждает публикацию" in auto.inner_text()
            assert "Адресат на паузе" in auto.inner_text()
            assert old_human.locator(".human-review input[type=checkbox]").count() == 1
            assert not old_human.locator(".human-review input").is_checked()
            assert old_human.get_by_role("button", name="Согласовать и поставить в план", exact=True).is_disabled()
            assert errors == [] and all(method == "GET" for method, _ in requests)
        finally:
            browser.close()
