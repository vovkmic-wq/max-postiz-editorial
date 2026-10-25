"""Точный MADLAD допустим только в автоматическом сценарии; остальные защиты прежние."""

import json

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.translation import (DedicatedTranslator, MADLAD_ARTIFACT_SHA256,
                                          MADLAD_BACKEND_CONTRACT, MADLAD_MODEL, translation_messages)

pytest_plugins = ["test_article_cycle"]


@pytest.mark.parametrize(("mode", "model", "allowed"), [
    ("automatic", MADLAD_MODEL, True),
    ("automatic", "hy-mt2-7b-q8", True),
    ("automatic", "hy-mt2-1.8b-q8", False),
    ("automatic", "translategemma-12b-q4", False),
    ("automatic", "opus-mt-en-ru-int8", False),
    ("automatic", None, False),
    ("human", "hy-mt2-7b-q8", True),
    ("human", MADLAD_MODEL, False),
])
def test_hourly_constructor_requires_explicit_allowed_translation(cycle, monkeypatch, mode, model, allowed):
    value, core, _, records, calls, _, policy = cycle
    value.quality_factory = None
    policy["approval_mode"] = mode
    if mode == "automatic":
        policy["review_rubric"] = "meaning-first-v1"
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    path = core.settings.root / "config/translation.json"
    if model is not None:
        path.write_text(json.dumps({"enabled": True, "model": model,
                                   "base_url": "http://127.0.0.1:8094"}))

    class Reviewer:
        def __init__(self, _):
            self.client = httpx.Client(transport=httpx.MockTransport(
                lambda _: pytest.fail("Конструктор не должен вызывать inference")))

    monkeypatch.setattr("max_postiz_agent.article_cycle.OpenCode", Reviewer)
    if not allowed:
        code = "ARTICLE_AUTOMATIC_TRANSLATOR_REQUIRED" if mode == "automatic" else "HUMAN_HY_TRANSLATOR_REQUIRED"
        with pytest.raises(AgentError, match=code):
            value._quality()
    else:
        quality = value._quality()
        try:
            assert isinstance(quality.translator, DedicatedTranslator)
            assert quality.translator.model == model and quality.translator.immutable_candidates is True
            assert quality.reviewer.model == "proxmox/qwen25-coder-7b"
            assert quality.reviewer is not quality.translator
            assert quality.review_rubric == ("meaning-first-v1" if mode == "automatic" else "legacy")
        finally:
            quality.close()
            quality.translator.client.close()
            quality.reviewer.client.close()
    assert not records and not calls  # Ни Postiz, ни публикации при подключении модели.


def test_old_madlad_alias_cannot_claim_the_corrected_backend():
    with pytest.raises(AgentError, match="TRANSLATION_CONFIG_INVALID"):
        DedicatedTranslator({"model": "madlad400-3b-q4k", "base_url": "http://127.0.0.1:8094"})
    with pytest.raises(AgentError, match="TRANSLATION_MODEL_INVALID"):
        translation_messages(MADLAD_MODEL, "English source.", "Unused chat prompt.")


@pytest.mark.parametrize("reply", [
    {"model": "madlad400-3b-q4k", "complete": True, "text": "Устаревшая сборка"},
    {"model": MADLAD_MODEL, "complete": False, "text": "Неполный перевод"},
    {"model": MADLAD_MODEL, "complete": True, "text": ""},
])
def test_madlad_wrong_backend_or_incomplete_output_is_not_a_translation(reply):
    translator = DedicatedTranslator({"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094"},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply)))
    try:
        with pytest.raises(AgentError, match="TRANSLATION_RESPONSE_INVALID"):
            translator.translate("The telescope observes stars.")
    finally:
        translator.client.close()


@pytest.mark.parametrize("change", ["missing", "contract", "worker", "model", "tokenizer", "config"])
def test_madlad_requires_pinned_artifacts_in_each_response(change):
    hashes = dict(MADLAD_ARTIFACT_SHA256)
    metrics = {"backend_contract": MADLAD_BACKEND_CONTRACT, "artifact_sha256": hashes}
    if change == "missing":
        metrics = {}
    elif change == "contract":
        metrics["backend_contract"] = "madlad400-before-causal-fix"
    else:
        hashes[change] = "0" * 64
    translator = DedicatedTranslator({"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094"},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "model": MADLAD_MODEL, "complete": True, "text": "Завершённый перевод.", "metrics": metrics})))
    try:
        with pytest.raises(AgentError, match="TRANSLATION_RESPONSE_INVALID"):
            translator.translate("The telescope observes stars.")
    finally:
        translator.client.close()


def test_madlad_keeps_backend_evidence_and_exact_instance_cache():
    requests = []
    metrics = {"backend_contract": MADLAD_BACKEND_CONTRACT,
               "artifact_sha256": dict(MADLAD_ARTIFACT_SHA256), "raw_worker_base64": "cHJpdmF0ZQ=="}

    def response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={"model": MADLAD_MODEL, "complete": True,
            "text": "Заголовок" if payload["text"] == "Title" else "Расстояние 1,5 км.", "metrics": metrics})

    translator = DedicatedTranslator({"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094"},
        transport=httpx.MockTransport(response))
    source = {"source_untrusted": {"title": "Title", "body": "Distance 1.5 km."}}
    try:
        first, evidence = translator.generate_text(json.dumps(source))
        second, cached = translator.generate_text(json.dumps({**source, "repair_feedback": "Not a new candidate"}))
        assert first == second and json.loads(first)["body"] == "Расстояние 1.5 км."
        assert evidence["segments"][1]["numeric_spelling_restored"] == 1
        assert evidence["segments"][1]["raw_worker_base64"] == metrics["raw_worker_base64"]
        assert evidence["segments"][1]["artifact_sha256"] == MADLAD_ARTIFACT_SHA256
        assert cached["cache_reused"] is True and evidence["cache_reused"] is False
        assert len(requests) == 2
    finally:
        translator.client.close()


def test_candidate_recheck_with_madlad_config_never_retranslates(cycle, monkeypatch):
    value, core, _, records, calls, _, policy = cycle
    value.quality_factory = None
    policy.update(approval_mode="automatic", review_rubric="meaning-first-v1")
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    (core.settings.root / "config/translation.json").write_text(json.dumps({
        "enabled": True, "model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094"}))

    class Reviewer:
        def __init__(self, _):
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No inference")))

    monkeypatch.setattr("max_postiz_agent.article_cycle.OpenCode", Reviewer)
    quality = value._quality(review_only=True)
    try:
        assert quality.translator.model == "human_editor_candidate"
        assert quality.reviewer.model == "proxmox/qwen25-coder-7b"
        with pytest.raises(AgentError, match="ARTICLE_TRANSLATION_NOT_ALLOWED"):
            quality.translator.generate_text("Existing candidate.")
    finally:
        quality.close()
        quality.translator.client.close()
        quality.reviewer.client.close()
    assert not records and not calls
