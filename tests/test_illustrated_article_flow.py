"""Новый формат проходит резерв и Postiz с одним неизменным изображением."""
import copy
import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from max_postiz_agent.article_attribution import ILLUSTRATED_PRESENTATION_CONTRACTS
from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Dispatch, TargetApproval, Variant
from max_postiz_agent.max_bridge import MaxBridge
from max_postiz_agent.postiz import PostizClient
from test_automatic_policy import Model

pytest_plugins = ["test_hourly_reserve_controller"]
CONTRACT = "article-illustrated-v1"


@pytest.fixture(params=sorted(ILLUSTRATED_PRESENTATION_CONTRACTS))
def illustrated(reserve_controller, monkeypatch, request):
    value = reserve_controller
    contract = request.param
    policy_path = value.core.settings.root / "data/hourly-article-policy.local.json"
    policy = json.loads(policy_path.read_text())
    policy.update(presentation_contract=contract,
        presentation_owner_instruction="Owner requires a licensed image above each new article")
    policy_path.write_text(json.dumps(policy))
    picture = io.BytesIO()
    Image.new("RGB", (1600, 900), "navy").save(picture, format="JPEG")
    raw = picture.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    folder = value.core.settings.root / value.core.settings.app["media_dir"]
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / (digest + ".jpg")
    image.write_bytes(raw)
    media = {"path": image.relative_to(value.core.settings.root).as_posix(), "sha256": digest, "mime": "image/jpeg"}
    binding = {"contract": contract, "credit": "Licensed fixture image, CC BY 4.0", "media": media,
               "width": 1600, "height": 900, "bytes": len(raw)}
    events = []

    def prepare(core, article, record, policy):
        assert record["presentation_contract"] == policy["presentation_contract"] == contract
        assert value.cycle._record(value.cycle._load(), record["id"])["preparation_attempts"]
        events.append("image")
        return copy.deepcopy(binding)

    def validate(core, record, variant=None):
        assert record["presentation_contract"] == contract
        assert record["illustration"] == binding
        assert record["illustration_proof_hash"] == fingerprint(binding)
        if variant is not None:
            assert variant.metadata_json["presentation_contract"] == contract
            assert variant.media == [media]
            assert variant.metadata_json["illustration"] == binding
            assert variant.metadata_json["illustration_proof_hash"] == fingerprint(binding)
        return [copy.deepcopy(media)]

    import sys
    import max_postiz_agent
    images = SimpleNamespace(prepare_article_image=prepare, validate_article_image=validate)
    monkeypatch.setitem(sys.modules, "max_postiz_agent.article_images", images)
    monkeypatch.setattr(max_postiz_agent, "article_images", images, raising=False)

    def quality(record_id=None, review_only=False):
        record = value.cycle._record(value.cycle._load(), record_id)
        assert events and events[0] == "image"
        events.append("models")
        return ArticleQuality(Model({"title": "Космический ветер",
            "body": "Учёные изучили необычные процессы в атмосфере далёкой звезды."}, "hy-mt2-7b-q8"),
            Model({"approved": True, "topic": "science", "issues": [], "warnings": []}, "ministral"),
            prompt_dir=value.core.settings.root / "prompts", trace=value.cycle._trace_writer(record_id),
            review_rubric="meaning-first-v1", presentation_contract=record["presentation_contract"],
            image_credit=record["image_credit"],
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))

    monkeypatch.setattr(value.cycle, "_quality", quality)
    monkeypatch.setattr(value.core.postiz, "upload", lambda *_: {"id": "image-fixture", "path": "/uploads/fixture.jpg"})
    original_create = value.core.postiz.create

    def create(*args, **kwargs):
        ident = original_create(*args, **kwargs)
        value.posts[ident]["image"] = json.dumps(args[2])
        return ident

    monkeypatch.setattr(value.core.postiz, "create", create)
    value.core.postiz.client.close()
    value.core.postiz.base = "http://127.0.0.1:9999/public/v1"
    value.core.postiz.client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=raw, headers={"content-type": "image/jpeg"})))
    value.media, value.binding, value.events, value.image_path = media, binding, events, image
    value.presentation_contract = contract
    return value


def test_illustrated_reserve_keeps_media_and_proof_through_one_draft_schedule_and_delivery(illustrated, monkeypatch):
    value = illustrated
    value.controller.prepare_once()
    ready = value.cycle._load()["records"][0]
    assert ready["state"] == "quality_ready" and value.events == ["image", "models"]
    assert ready["presentation_contract"] == value.presentation_contract
    assert ready["preparation_attempts"][0]["presentation_contract"] == value.presentation_contract
    assert not value.posts and not value.writes
    with value.core.db.session() as session:
        variant = session.get(Variant, ready["variant_id"])
        assert variant.media == [value.media]
        assert "Машинный перевод на русский и автоматическая редактура." not in variant.body
        assert "Перевод и адаптация." in variant.body
        assert value.binding["credit"] in variant.body
        audit = session.query(Audit).filter_by(action="hourly_article_prepare_attempt", entity=ready["id"]).one()
        assert audit.details["illustration_proof_hash"] == fingerprint(value.binding)
        assert audit.details["presentation_contract"] == value.presentation_contract
    trace = value.core.settings.root / ready["quality_trace"]
    assert all(json.loads(line)["presentation_contract"] == value.presentation_contract
               for line in trace.read_text(encoding="utf-8").splitlines())
    value.controller.run_once()
    pending = value.cycle._load()["records"][0]
    assert pending["state"] == "pending" and len(value.posts) == len(value.writes) == 1
    remote = value.posts[pending["postiz_id"]]
    assert remote["state"] == "QUEUE" and len(json.loads(remote["image"])) == 1
    with value.core.db.session() as session:
        dispatch = session.query(Dispatch).one()
        assert dispatch.snapshot["media"] == [value.media]
        assert session.query(TargetApproval).count() == 1
    from max_postiz_agent.adapters import Delivery
    uploaded, delivered = [], []

    def upload(path):
        assert path == value.image_path
        uploaded.append(path)
        return {"type": "browser-image", "payload": {key: value.media[key] for key in ("path", "sha256")}}

    def publish(destination, body, attachment, **kwargs):
        assert attachment["type"] == "browser-image"
        assert attachment["payload"]["sha256"] == value.media["sha256"]
        assert value.binding["credit"] in body and kwargs["request_id"] == pending["dispatch_id"]
        delivered.append((destination, body, attachment))
        return Delivery("confirmed", "fixture-delivered-image", None)

    value.core.publisher.mode = "browser"
    monkeypatch.setattr(value.core.publisher, "upload_image", upload, raising=False)
    monkeypatch.setattr(value.core.publisher, "publish", publish)
    value.stamp[0] = pending["due_at"] + 1
    assert value.core.dispatch_tick() == 1
    assert value.core.dispatch_tick() == 0
    assert len(uploaded) == len(delivered) == 1
    value.posts[pending["postiz_id"]]["state"] = "PUBLISHED"
    value.controller.run_once()
    assert value.cycle._load()["records"][0]["state"] == "published"


def test_changed_illustration_proof_is_blocked_before_remote_staging(illustrated):
    value = illustrated
    value.controller.prepare_once()
    ledger = value.cycle._load()
    ledger["records"][0]["illustration_proof_hash"] = "0" * 64
    value.cycle._save(ledger)
    with pytest.raises((AgentError, AssertionError)):
        value.controller.run_once()
    assert not value.posts and not value.writes


def test_changed_image_bytes_are_blocked_before_remote_staging(illustrated):
    value = illustrated
    value.controller.prepare_once()
    value.image_path.write_bytes(b"changed bytes")
    with pytest.raises(AgentError, match="MEDIA_HASH_CHANGED"):
        value.controller.run_once()
    assert not value.posts and not value.writes


def test_exact_illustrated_pending_resumes_after_pause_without_upload_or_new_schedule(illustrated):
    value = illustrated
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    before = value.store.get(value.store.status()["active"][0]["id"])
    value.state.control("pause", value.state.read()["revision"], "fixture-owner")
    value.state.control("start", value.state.read()["revision"], "fixture-owner")
    assert value.controller._resume_pending(record, record["dispatch_id"],
        value.state.read()["settings"], value.cycle._policy_for_record(record))
    after = value.store.get(before["id"])
    assert after["control_revision"] == value.state.read()["revision"]
    assert after["token"] == before["token"] and after["due_at"] == before["due_at"]
    assert len(value.posts) == len(value.writes) == 1
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1


def cancelled_image(value):
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    value.stamp[0] = record["due_at"] + 1
    value.posts[record["postiz_id"]]["state"] = "ERROR"
    value.core.bridge.sync(force=True)
    assert value.cycle.status(record["id"])["state"] == "cancelled"
    value.state.control("pause", value.state.read()["revision"], "fixture-owner")
    value.state.control("start", value.state.read()["revision"], "fixture-owner")
    record = copy.deepcopy(value.cycle._load()["records"][0])
    slot = value.store.get(value.store.status()["active"][0]["id"])
    return record, slot, copy.deepcopy(value.posts[record["postiz_id"]])


def test_exact_cancelled_image_without_attempt_can_release_only_its_slot(illustrated):
    value = illustrated
    record, slot, remote = cancelled_image(value)
    assert value.store.release_cancelled_no_send(slot["id"], token=slot["token"], remote=remote,
        expected_revision=value.state.read()["revision"])["state"] == "missed"
    assert value.cycle._load()["records"][0] == record
    assert value.store.status()["active"] == []
    assert len(value.posts) == len(value.writes) == 1
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, record["dispatch_id"])
        assert dispatch.state == "cancelled" and dispatch.attempts == 0


@pytest.mark.parametrize("damage", ["media_url", "image_missing", "proof", "receipt"])
def test_uncertain_cancelled_image_never_frees_slot(illustrated, damage):
    value = illustrated
    record, slot, remote = cancelled_image(value)
    if damage == "media_url":
        remote["image"] = [{"id": "image-fixture", "path": "/uploads/changed.jpg"}]
    elif damage == "image_missing":
        remote["image"] = []
    elif damage == "proof":
        ledger = value.cycle._load()
        ledger["records"][0]["illustration_proof_hash"] = "0" * 64
        value.cycle._save(ledger)
    else:
        receipt = value.core.settings.root / "data/browser-publications" / (record["dispatch_id"] + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text('{"state":"sending"}')
    with pytest.raises((AgentError, AssertionError)):
        value.store.release_cancelled_no_send(slot["id"], token=slot["token"], remote=remote,
            expected_revision=value.state.read()["revision"])
    assert value.store.get(slot["id"])["state"] == "pending"
    assert len(value.posts) == len(value.writes) == 1


@pytest.mark.parametrize("presentation", [
    {"presentation_contract": CONTRACT},
    {"presentation_owner_instruction": "owner requires licensed images"},
    {"presentation_contract": "unknown-v2", "presentation_owner_instruction": "owner requires licensed images"},
    {"presentation_contract": CONTRACT, "presentation_owner_instruction": None},
    {"presentation_contract": CONTRACT, "presentation_owner_instruction": "short"},
])
def test_incomplete_or_unknown_policy_contract_does_not_call_models(reserve_controller, presentation):
    value = reserve_controller
    path = value.core.settings.root / "data/hourly-article-policy.local.json"
    policy = json.loads(path.read_text())
    policy.update(presentation)
    path.write_text(json.dumps(policy))
    with pytest.raises(AgentError, match="ARTICLE_POLICY_INVALID"):
        value.cycle._policy()
    assert not value.model_calls and not value.posts


@pytest.mark.parametrize("contract", sorted(ILLUSTRATED_PRESENTATION_CONTRACTS))
def test_new_policy_holds_legacy_ready_and_refills_only_sendable_images(reserve_controller, contract):
    value = reserve_controller
    value.controller.prepare_once()
    second = {**value.article, "url": "https://science.example.org/article/legacy-second/",
        "original": value.article["original"] + " A separate historical observation."}
    value.controller.selector = lambda *_args, **kwargs: (copy.deepcopy(second) if not any(
        r["url"] == second["url"] for r in kwargs["ledger"]["records"]) else None)
    value.controller.prepare_once()
    original = copy.deepcopy(value.cycle._load()["records"])
    path = value.core.settings.root / "data/hourly-article-policy.local.json"
    policy = json.loads(path.read_text())
    policy.update(presentation_contract=contract,
        presentation_owner_instruction="Owner requires a licensed image above every new article")
    path.write_text(json.dumps(policy))
    assert value.controller.run_once()["phase"] == "reserve_empty"
    value.state.control("pause", value.state.read()["revision"], "fixture-owner")
    settings = copy.deepcopy(value.state.read()["settings"])
    settings["reserve"].update(target=2, low=1)
    value.state.settings(settings, value.state.read()["revision"], "fixture-owner")
    value.state.control("start", value.state.read()["revision"], "fixture-owner")
    # Два старых ready уже заполнили бы target=2 при неправильном подсчёте.
    assert value.controller.prepare_once()["phase"] == "no_source"
    assert value.cycle._load()["records"] == original
    assert value.store.active_preparation() is None
    assert not value.posts and not value.writes


def test_missing_image_acquisition_never_calls_translation_or_postiz(illustrated, monkeypatch):
    import max_postiz_agent.article_images as images
    value = illustrated

    def missing(*_args):
        raise AgentError("ARTICLE_IMAGE_REQUIRED", 409)

    monkeypatch.setattr(images, "prepare_article_image", missing)
    assert value.controller.prepare_once()["phase"] == "no_source"
    assert not value.events and not value.posts and not value.writes
    record = value.cycle._load()["records"][0]
    assert record["state"] == "cancelled" and record["error"] == "ARTICLE_IMAGE_REQUIRED"
    assert value.store.active_preparation() is None
    with value.core.db.session() as session:
        audit = session.query(Audit).filter_by(action="reserve_image_rejected_before_models").one()
        assert audit.details["model_calls"] == 0 and audit.details["source_remains_used"]


def test_known_image_failure_continues_with_a_different_source_in_the_same_batch(illustrated, monkeypatch):
    import max_postiz_agent.article_images as images
    value = illustrated
    second = {**value.article, "url": "https://science.example.org/article/second/",
        "original": value.article["original"] + " Additional measurements explain the stellar activity."}
    original = images.prepare_article_image

    def prepare(core, article, record, policy):
        if article["url"] == value.article["url"]:
            raise AgentError("ARTICLE_IMAGE_RIGHTS_REQUIRED", 409)
        return original(core, article, record, policy)

    def select(_settings, *, ledger, **_):
        used = {r["url"] for r in ledger["records"]}
        return copy.deepcopy(next((a for a in (value.article, second) if a["url"] not in used), None))

    monkeypatch.setattr(images, "prepare_article_image", prepare)
    value.controller.selector = select
    value.controller.prepare_once()
    records = value.cycle._load()["records"]
    assert [r["state"] for r in records] == ["cancelled", "quality_ready"]
    assert [r["url"] for r in records] == [value.article["url"], second["url"]]
    assert value.events == ["image", "models"] and value.store.active_preparation() is None
    assert not value.posts and not value.writes


def test_bounded_meaning_repair_keeps_the_same_illustration_and_new_footer(illustrated, monkeypatch):
    from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
    from test_automatic_article_repair import patched
    value = illustrated
    original = value.cycle._quality
    reviews, edits = [], []

    def quality(record_id=None, review_only=False):
        result = original(record_id, review_only)
        reviews.append(record_id)
        if len(reviews) == 1:
            result.reviewer.value = {"approved": False, "topic": "science",
                "issues": ["The discovery of the stellar wind is omitted."], "warnings": []}
        return result

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            context = json.loads(prompt.split("UNTRUSTED_JSON_DATA:\n", 1)[1])
            edits.append(context)
            body = "Учёные изучили необычные процессы в атмосфере далёкой звезды и обнаружили звёздный ветер."
            return json.dumps(patched(context, body), ensure_ascii=False), {}

    monkeypatch.setattr(value.cycle, "_quality", quality)
    value.cycle.repair_factory = lambda trace, **presentation: ArticleRepair(
        Editor(), prompt_dir=value.core.settings.root / "prompts", trace=trace, **presentation)
    value.controller.prepare_once()
    record = value.cycle._load()["records"][0]
    assert record["state"] == "quality_ready"
    assert len(edits) == 1 and len(reviews) == 2
    assert record["illustration"] == value.binding
    claim = record["automatic_repair_attempts"][0]
    assert claim["state"] == "passed" and claim["presentation_contract"] == value.presentation_contract
    assert claim["image_credit"] == value.binding["credit"]
    value.controller.run_once()
    assert value.cycle._load()["records"][0]["state"] == "pending"


@pytest.mark.parametrize("damage", ["proof_error", "model_unknown", "trace", "model_started", "partial_proof"])
def test_unknown_or_damaged_image_preparation_keeps_claim_and_never_repeats_get(illustrated, monkeypatch, damage):
    import max_postiz_agent.article_images as images
    value = illustrated
    calls = []

    def broken(core, article, record, policy):
        calls.append(record["id"])
        if damage == "model_unknown":
            record["model_result_unknown"] = True
        elif damage == "trace":
            trace = value.core.settings.root / record["quality_trace"]
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_text('{"phase":"translation_started"}')
        elif damage == "model_started":
            record["preparation_attempts"][0]["model_started_at"] = value.stamp[0]
        elif damage == "partial_proof":
            record["illustration"] = {}
        raise AgentError("ARTICLE_IMAGE_PROOF_INVALID" if damage == "proof_error" else "ARTICLE_IMAGE_REQUIRED", 409)

    monkeypatch.setattr(images, "prepare_article_image", broken)
    with pytest.raises(AgentError):
        value.controller.prepare_once()
    assert value.store.active_preparation()["state"] == "blocked_unknown"
    assert value.controller.prepare_once()["phase"] == "blocked"
    assert len(calls) == 1 and not value.events and not value.posts and not value.writes


def test_image_upload_unknown_is_never_repeated(illustrated, monkeypatch):
    value = illustrated
    value.controller.prepare_once()
    calls = []

    def unknown(*args):
        calls.append(args)
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(value.core.postiz, "upload", unknown)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.controller.run_once()
    value.controller.run_once()
    assert len(calls) == 1 and not value.posts and not value.writes


@pytest.fixture
def image_schedule_client(monkeypatch):
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1:9999/public/v1")
    monkeypatch.setenv("POSTIZ_API_KEY", "fixture-key")
    remote = {"id": "same-post", "state": "DRAFT", "content": "Exact caption",
        "image": [{"id": "image-id", "path": "/uploads/original.jpg"}], "childrenPost": [],
        "publishDate": "2026-10-28T13:00:00Z", "group": "same-group",
        "integration": {"id": "integration-max"}, "settings": {"__type": "max-browser"}}
    calls, answer = [], [[{"postId": "same-post"}]]

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"posts": [copy.deepcopy(remote)]})
        if isinstance(answer[0], Exception):
            raise answer[0]
        return httpx.Response(200, json=answer[0], headers={
            "X-Max-Postiz-CAS-Verified": request.headers.get("X-Max-Postiz-Expected-Hash", "")})

    client = PostizClient({}, httpx.MockTransport(respond))
    media = [{"path": "data/media/" + "a" * 64 + ".jpg", "sha256": "a" * 64, "mime": "image/jpeg"}]
    yield client, remote, media, calls, answer
    client.close()


def test_existing_illustrated_draft_preserves_uploaded_image_with_exact_cas(image_schedule_client):
    client, remote, media, calls, _ = image_schedule_client
    hashed = MaxBridge.normalize(remote)[3]
    assert client.reschedule_existing("same-post", "Exact caption", 1793196763,
        expected_media=media, expected_remote_hash=hashed) == "same-post"
    assert [r.method for r in calls] == ["GET", "POST"]
    body = json.loads(calls[-1].content)
    assert body["posts"][0]["value"][0]["image"] == remote["image"]
    assert calls[-1].headers["X-Max-Postiz-Expected-Hash"] == hashed


@pytest.mark.parametrize("change", ["image", "body", "state", "group", "date", "integration"])
def test_changed_image_or_remote_binding_never_schedules(image_schedule_client, change):
    client, remote, media, calls, _ = image_schedule_client
    hashed = MaxBridge.normalize(remote)[3]
    if change == "image":
        remote["image"][0]["path"] = "/uploads/other.jpg"
    elif change == "body":
        remote["content"] += " changed"
    elif change == "state":
        remote["state"] = "PUBLISHED"
    elif change == "group":
        remote["group"] = "other-group"
    elif change == "date":
        remote["publishDate"] = "2026-10-28T14:00:00Z"
    else:
        remote["integration"]["id"] = "other-target"
    with pytest.raises(AgentError):
        client.reschedule_existing("same-post", "Exact caption", 1793196763,
            expected_media=media, expected_remote_hash=hashed)
    assert [r.method for r in calls] == ["GET"]


def test_legacy_reschedule_still_rejects_any_image(image_schedule_client):
    client, _, _, calls, _ = image_schedule_client
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_CHANGED"):
        client.reschedule_existing("same-post", "Exact caption", 1793196763)
    assert [r.method for r in calls] == ["GET"]


@pytest.mark.parametrize("answer", [[], [{"postId": "other-post"}], httpx.ReadTimeout("opaque timeout")])
def test_illustrated_schedule_unknown_is_not_retried(image_schedule_client, answer):
    client, remote, media, calls, result = image_schedule_client
    result[0] = answer
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        client.reschedule_existing("same-post", "Exact caption", 1793196763,
            expected_media=media, expected_remote_hash=MaxBridge.normalize(remote)[3])
    assert [r.method for r in calls] == ["GET", "POST"]
