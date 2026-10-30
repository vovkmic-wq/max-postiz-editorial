"""Новый source-bound словарь закреплён до модели и не меняет старые отказы."""
import copy
import hashlib
import json

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import scientific_contract_binding
from max_postiz_agent.article_quality import ArticleQuality, rubric_spelling
from max_postiz_agent.article_repair_cycle import AutomaticArticleRepair
from max_postiz_agent.automatic_policy import automatic_evidence, scientific_trace_binding
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.scientific_terms import SCIENTIFIC_SPELLING_CONTRACT
from test_automatic_policy import Model
from test_scientific_terms import finding

pytest_plugins = ["test_automatic_policy"]

TITLE = "Наблюдения космического телескопа «Хаббл»"
BODY = "Учёные изучили необычные процессы в атмосфере далёкой звезды и обнаружили звёздный ветер."
ORIGINAL = ("The Hubble Space Telescope observed a distant star. Scientists studied its unusual atmosphere "
            "and discovered a stellar wind. Observations help scientists understand distant stars.")


def install_quality(values, review, monkeypatch, *, registered=True, typo=False, source=ORIGINAL):
    cycle, core, article, _, _, _, _ = values
    article.update(title="Hubble Space Telescope observations", original=source)
    cycle.scientific_spelling_contract = SCIENTIFIC_SPELLING_CONTRACT if registered else None
    counts = {"translation": 0, "review": 0, "spelling": 0}
    body = BODY + (" Ошипка." if typo else "")

    class CountedModel(Model):
        def generate_text(self, prompt):
            counts["translation" if self.model == "hy-mt2-7b-q8" else "review"] += 1
            # До первого запроса уже существуют durable record, attempt и Audit.
            record = cycle._load()["records"][0]
            with core.db.session() as session:
                assert scientific_contract_binding(core, session, record) == cycle.scientific_spelling_contract
            return super().generate_text(prompt)

    def spelling(request):
        counts["spelling"] += 1
        text = TITLE + "\n\n" + body
        matches = [finding(text, "Хаббл")]
        if typo:
            matches.append(finding(text, "Ошипка"))
        return httpx.Response(200, json={"matches": matches})

    def quality(record_id=None, review_only=False):
        return ArticleQuality(CountedModel({"title": TITLE, "body": body}, "hy-mt2-7b-q8"),
            CountedModel(copy.deepcopy(review), "ministral"), prompt_dir=core.settings.root / "prompts",
            trace=cycle._trace_writer(record_id), review_rubric="meaning-first-v1",
            transport=httpx.MockTransport(spelling), allow_registered_terms=registered)

    monkeypatch.setattr(cycle, "_quality", quality)
    return counts


@pytest.fixture
def registered_ready(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    counts = install_quality(values, review, monkeypatch)
    cycle, core, article, records, writes, _, _ = values
    ready = cycle.prepare(article)
    return values, ready, counts


def test_source_bound_fresh_pipeline_metadata_trace_and_policy_accept_exact_contract(registered_ready):
    values, ready, counts = registered_ready
    cycle, core, _, records, writes, _, _ = values
    assert counts == {"translation": 1, "review": 1, "spelling": 1}
    assert ready["state"] == "automatic_ready" and len(records) == len(writes) == 1
    record = cycle._load()["records"][0]
    assert record["scientific_spelling_contract"] == SCIENTIFIC_SPELLING_CONTRACT
    with core.db.session() as session:
        variant = session.get(Variant, ready["variant_id"])
        assert scientific_contract_binding(core, session, record, variant) == SCIENTIFIC_SPELLING_CONTRACT
        evidence = variant.metadata_json["machine_review_evidence"]
        assert evidence["scientific_spelling_contract"] == SCIENTIFIC_SPELLING_CONTRACT
        assert evidence["spelling_unresolved"] == []
        assert evidence["spelling_adjudicated"][0]["word"] == "Хаббл"
        assert automatic_evidence(session, variant) is not None
    planned = cycle.publish(ready["id"], ready["content_hash"])
    assert planned["state"] == "pending"


def test_mixed_lt_real_typo_still_rejected_and_repair_replays_exact_new_rubric(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    counts = install_quality(values, review, monkeypatch, typo=True)
    cycle, core, article, records, writes, _, _ = values
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    record = cycle._load()["records"][0]
    payload = AutomaticArticleRepair(cycle)._payload(record)
    assert payload["scientific_spelling_contract"] == SCIENTIFIC_SPELLING_CONTRACT
    assert cycle.automatic_repair_status(record) == {"eligible": True, "reason": "READY", "attempts": 0}
    spelling = next(event for event in payload["feedback"]["events"] if event["phase"] == "spelling_adjudicated")
    assert spelling["adjudicated"][0]["word"] == "Хаббл"
    assert len(spelling["unresolved"]) == 1
    assert spelling["unresolved"][0]["message"] == "Unknown spelling"
    assert counts == {"translation": 1, "review": 1, "spelling": 1} and not records and not writes


def test_old_record_without_marker_retains_original_refusal_and_legacy_repair(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    counts = install_quality(values, review, monkeypatch, registered=False)
    cycle, core, article, records, writes, _, _ = values
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    record = cycle._load()["records"][0]
    before = (core.settings.root / record["quality_trace"]).read_bytes()
    cycle.scientific_spelling_contract = SCIENTIFIC_SPELLING_CONTRACT
    payload = AutomaticArticleRepair(cycle)._payload(record)
    assert "scientific_spelling_contract" not in record and "scientific_spelling_contract" not in payload
    spelling = next(event for event in payload["feedback"]["events"] if event["phase"] == "spelling_adjudicated")
    assert spelling["adjudicated"] == [] and len(spelling["unresolved"]) == 1
    assert (core.settings.root / record["quality_trace"]).read_bytes() == before
    assert counts == {"translation": 1, "review": 1, "spelling": 1} and not records and not writes


def test_mixed_rejection_correction_keeps_new_contract_and_original_raw(automatic_cycle, monkeypatch):
    from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
    values, review = automatic_cycle
    first_counts = install_quality(values, review, monkeypatch, typo=True)
    cycle, core, article, records, writes, stamp, _ = values
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    initial = cycle._load()["records"][0]
    trace = core.settings.root / initial["quality_trace"]
    original_raw = trace.read_bytes()
    editor_calls = []

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            editor_calls.append(prompt)
            patch = {"candidate": {"title": TITLE, "body": BODY}, "changes": [{"field": "body",
                "before": BODY + " Ошипка.", "after": BODY,
                "source_quote": "Scientists studied its unusual atmosphere and discovered a stellar wind.",
                "reason": "Удалено ошибочно добавленное слово; исходные факты сохранены."}]}
            return json.dumps(patch, ensure_ascii=False), {}

    cycle.repair_factory = lambda trace: ArticleRepair(Editor(), prompt_dir=core.settings.root / "prompts", trace=trace)
    second_counts = install_quality(values, review, monkeypatch)
    stamp[0] += 1
    ready = cycle.repair_rejected(initial["id"])
    assert ready["state"] == "automatic_ready" and len(records) == len(writes) == len(editor_calls) == 1
    assert first_counts == {"translation": 1, "review": 1, "spelling": 1}
    assert second_counts == {"translation": 0, "review": 1, "spelling": 1}
    record = cycle._load()["records"][0]
    assert record["automatic_repair_attempts"][0]["state"] == "passed"
    assert len(record["preparation_attempts"]) == 2 and trace.read_bytes().startswith(original_raw)
    with core.db.session() as session:
        variant = session.get(Variant, ready["variant_id"])
        assert scientific_contract_binding(core, session, record, variant) == SCIENTIFIC_SPELLING_CONTRACT
        assert automatic_evidence(session, variant) is not None
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


@pytest.mark.parametrize("layer", ["record", "attempt", "audit", "metadata", "evidence"])
@pytest.mark.parametrize("value", [None, True, "unregistered-v9"])
def test_any_missing_or_changed_durable_contract_blocks_publish_without_new_remote_write(registered_ready, layer, value):
    values, ready, _ = registered_ready
    cycle, core, _, records, writes, _, _ = values
    ledger = cycle._load()
    if layer in {"record", "attempt"}:
        target = ledger["records"][0] if layer == "record" else ledger["records"][0]["preparation_attempts"][0]
        if value is None:
            target.pop("scientific_spelling_contract")
        else:
            target["scientific_spelling_contract"] = value
        cycle._save(ledger)
    else:
        with core.db.session.begin() as session:
            variant = session.get(Variant, ready["variant_id"])
            if layer == "audit":
                audit = session.scalar(select(Audit).where(Audit.entity == ready["id"],
                    Audit.action == "hourly_article_prepare_attempt"))
                target = copy.deepcopy(audit.details)
            elif layer == "metadata":
                target = copy.deepcopy(variant.metadata_json)
            else:
                target = copy.deepcopy(variant.metadata_json["machine_review_evidence"])
            if value is None:
                target.pop("scientific_spelling_contract")
            else:
                target["scientific_spelling_contract"] = value
            if layer == "audit":
                audit.details = target
            elif layer == "metadata":
                variant.metadata_json = target
            else:
                variant.metadata_json = {**variant.metadata_json, "machine_review_evidence": target,
                                         "machine_review_evidence_hash": fingerprint(target)}
    before = copy.deepcopy(records), list(writes)
    with pytest.raises(AgentError):
        cycle.publish(ready["id"], ready["content_hash"])
    assert (records, writes) == before
    with core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


@pytest.mark.parametrize("change", ["start", "spelling", "order", "hash", "accepted", "source", "record_id", "lt_raw", "review_raw"])
def test_trace_marker_hash_and_existing_phase_order_are_required(registered_ready, change):
    values, ready, _ = registered_ready
    cycle, core, _, _, _, _, _ = values
    record = cycle._load()["records"][0]
    path = core.settings.root / record["quality_trace"]
    previous_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if change == "start":
        events[0]["context"].pop("scientific_spelling_contract")
    elif change == "spelling":
        next(event for event in events if event["phase"] == "spelling_adjudicated").pop("scientific_spelling_contract")
    elif change == "order":
        events[-3], events[-2] = events[-2], events[-3]
    elif change == "accepted":
        events[-1]["result"].pop("scientific_spelling_contract")
    elif change == "source":
        events[0]["context"]["source_untrusted"]["title"] = "Another source title"
    elif change == "record_id":
        events[1]["record_id"] = "another-record"
    elif change == "lt_raw":
        next(event for event in events if event["phase"] == "spelling_returned")["matches"] = []
    elif change == "review_raw":
        next(event for event in events if event["phase"] == "review_returned")["raw"] = "{}"
    else:
        events[-1]["result"]["topic"] = "changed"
    path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    expected_hash = previous_hash if change == "hash" else hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(AgentError, match="ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID"):
        scientific_trace_binding(path, SCIENTIFIC_SPELLING_CONTRACT, expected_hash=expected_hash)


@pytest.mark.parametrize("change", ["source", "rule", "offset", "grammar"])
def test_registered_terms_cannot_hide_source_rule_offset_or_grammar_mismatch(change):
    translated = TITLE + "\n\n" + BODY
    match = finding(translated, "Хаббл")
    source = "Hubble Space Telescope\n\n" + ORIGINAL
    if change == "source":
        source = "Scientists observed a distant star with an unnamed instrument."
    elif change == "rule":
        match["rule"]["id"] = "UNRELATED_RULE"
    elif change == "offset":
        match["offset"] += 1
    else:
        match["rule"]["issueType"] = "grammar"
    adjudicated, unresolved = rubric_spelling(source, translated, [match], "meaning-first-v1",
                                             allow_spacetime=True, allow_registered_terms=True)
    assert not adjudicated and unresolved == [match]


def test_explicit_contract_cannot_silently_use_legacy_quality_factory(automatic_cycle):
    values, _ = automatic_cycle
    cycle, core, article, records, writes, _, _ = values
    cycle.scientific_spelling_contract = SCIENTIFIC_SPELLING_CONTRACT
    with pytest.raises(AgentError, match="ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID"):
        cycle.prepare(article)
    assert not records and not writes


@pytest.mark.parametrize("field", ["source_hash", "input_hash", "policy_hash", "candidate_hash"])
def test_prepare_audit_must_bind_exact_source_input_policy_and_candidate(registered_ready, field):
    values, ready, _ = registered_ready
    cycle, core, _, records, writes, _, _ = values
    with core.db.session.begin() as session:
        audit = session.scalar(select(Audit).where(Audit.entity == ready["id"],
            Audit.action == "hourly_article_prepare_attempt"))
        audit.details = {**audit.details, field: "0" * 64}
    before = copy.deepcopy(records), list(writes)
    with pytest.raises(AgentError, match="ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID"):
        cycle.publish(ready["id"], ready["content_hash"])
    assert (records, writes) == before
