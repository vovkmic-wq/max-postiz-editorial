"""A new rubric can classify one exact old draft once, without granting delivery."""
import copy
import hashlib
import json

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.automatic_migration import RUBRIC, migrate
from max_postiz_agent.automatic_policy import automatic_evidence, eligible
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, uid
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant, VariantRevision
from max_postiz_agent.human_review import CandidateOnlyTranslator

pytest_plugins = ["test_hourly_human_review"]
REASON = "Owner explicitly requested a new meaning-first rubric for this exact existing article."
OLD_REVIEW = {"approved": False, "topic": "science", "issues": ["The old style refusal must stay unchanged."]}
NEW_REVIEW = {"approved": True, "topic": "science", "issues": [], "warnings": ["Optional wording suggestion."]}
RU_TITLE = "Необычный звёздный ветер"
RU_BODY = "Учёные изучили необычную атмосферу и обнаружили звёздный ветер у далёкой звезды."


@pytest.fixture
def migration(human_cycle, monkeypatch):
    cycle, core, article, records, writes, stamp, policy = human_cycle
    article["original"] = article["original"].strip()
    old_events = []

    class OldReviewer:
        model = "ministral-fixture"

        def generate_text(self, _):
            return json.dumps(OLD_REVIEW), {}

    def initial(record_id=None, **_):
        trace = cycle._trace_writer(record_id)

        def observe(event):
            old_events.append(copy.deepcopy(event))
            trace(event)

        quality = ArticleQuality(CandidateOnlyTranslator(), OldReviewer(),
            prompt_dir=core.settings.root / "prompts", trace=observe, allow_human_rejection=True,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))

        class Initial:
            def prepare(self, title, original, url):
                return quality.check_candidate(RU_TITLE, RU_BODY, title, original, url)

            def close(self):
                quality.close()

        return Initial()

    monkeypatch.setattr(cycle, "_quality", initial)
    prepared = cycle.prepare(article)
    old_trace = core.settings.root / cycle._load()["records"][0]["quality_trace"]
    before = cycle._snapshot(cycle._load()["records"][0])
    old_bytes = old_trace.read_bytes()
    policy["approval_mode"] = "automatic"
    policy["review_rubric"] = RUBRIC
    policy["owner_instruction"] = "Owner explicitly resumed automatic publication with meaning-first review."
    policy_path = core.settings.root / "data/hourly-article-policy.local.json"
    policy_path.write_text(json.dumps(policy))
    # Existing old-hour drafts may be classified now; migration does not claim a send slot.
    stamp[0] += 7200
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    result = {"cycle": cycle, "core": core, "article": article, "records": records,
        "writes": writes, "stamp": stamp, "policy": policy, "policy_path": policy_path,
        "prepared": prepared, "before": before, "old_trace": old_trace, "old_bytes": old_bytes,
        "review": copy.deepcopy(NEW_REVIEW), "review_calls": [], "spelling_calls": [],
        "matches": [], "on_review": None, "failure": None}

    class NewReviewer:
        model = "ministral-fixture"

        def generate_text(self, prompt):
            result["review_calls"].append(json.loads(prompt))
            if result["on_review"]:
                result["on_review"]()
            if result["failure"]:
                raise result["failure"]
            return json.dumps(result["review"]), {}

    def check(record_id=None, review_only=False):
        assert review_only is True and record_id != prepared["id"]

        def spelling(request):
            result["spelling_calls"].append(request)
            return httpx.Response(200, json={"matches": result["matches"]})

        return ArticleQuality(CandidateOnlyTranslator(), NewReviewer(),
            prompt_dir=core.settings.root / "prompts", trace=cycle._trace_writer(record_id),
            review_rubric=RUBRIC, transport=httpx.MockTransport(spelling))

    monkeypatch.setattr(cycle, "_quality", check)
    return result


def run(value):
    return migrate(value["cycle"], value["prepared"]["id"], REASON)


def assert_no_delivery(value):
    with value["core"].db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert all(target.paused for target in session.scalars(select(Target)))
    assert len(value["writes"]) == len(value["records"]) == 1


@pytest.mark.parametrize("state", ["DRAFT", "QUEUE"])
def test_one_new_review_preserves_old_refusal_and_same_post_without_scheduling(migration, state):
    value = migration
    value["records"][value["prepared"]["postiz_id"]]["state"] = state
    value["core"].bridge.sync(force=True)
    old = copy.deepcopy(value["cycle"]._load()["records"][0])
    remote = copy.deepcopy(value["records"])
    sibling = {"id": uid(), "slot": old["slot"] - 1, "state": "awaiting_human_review",
               "url": "https://science.example.org/another/", "translation_hash": "unrelated"}
    ledger = value["cycle"]._load()
    ledger["records"].append(copy.deepcopy(sibling))
    value["cycle"]._save(ledger)
    before = value["cycle"]._snapshot(old)
    result = run(value)
    assert result["id"] == old["id"] and result["state"] == "automatic_ready"
    assert result["automatic_enrolled"] is True and result["slot"] == old["slot"]
    assert "automatic_slot" not in result and "due_at" not in result and "dispatch_id" not in result
    assert result["postiz_id"] == old["postiz_id"] and value["records"] == remote
    assert value["old_trace"].read_bytes() == value["old_bytes"]
    saved = value["cycle"]._load()["records"]
    assert saved[1] == sibling
    attempt = saved[0]["automatic_migrations"][0]
    assert attempt["state"] == "completed" and attempt["reason"] == REASON
    assert attempt["previous_metadata"] == before["metadata"]
    backup = value["core"].settings.root / attempt["previous_trace_backup"]
    assert backup.read_bytes() == value["old_bytes"]
    assert attempt["previous_trace_hash"] == hashlib.sha256(value["old_bytes"]).hexdigest()
    with value["core"].db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert (variant.title, variant.body) == (before["title"], before["body"])
        assert variant.metadata_json["approval_mode"] == "automatic"
        assert variant.metadata_json["machine_review_evidence"]["review"] == NEW_REVIEW
        assert variant.metadata_json["automatic_migration_history"][0]["previous_metadata"]["machine_review_evidence"]["review"] == OLD_REVIEW
        assert automatic_evidence(session, variant) is not None
        assert session.scalar(select(Audit).where(Audit.action == "hourly_article_automatic_migrated"))
        assert session.scalar(select(VariantRevision).where(VariantRevision.variant_id == variant.id,
            VariantRevision.revision == variant.revision - 1)).payload["metadata"]["machine_review_status"] == "rejected"
    assert len(value["review_calls"]) == len(value["spelling_calls"]) == 1
    context = value["review_calls"][0]
    assert context["source_untrusted"] == value["article"]["title"] + "\n\n" + value["article"]["original"]
    assert context["translation_untrusted"] == RU_TITLE + "\n\n" + RU_BODY
    assert context["review_rubric"] == RUBRIC
    assert_no_delivery(value)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_NOT_SAFE"):
        run(value)
    assert len(value["review_calls"]) == 1


def test_negative_new_rubric_stays_negative_and_cannot_be_resampled(migration):
    value = migration
    value["review"] = {**OLD_REVIEW, "warnings": []}
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        run(value)
    record = value["cycle"]._load()["records"][0]
    assert record["state"] == "awaiting_human_review" and not record.get("automatic_enrolled")
    attempt = record["automatic_migrations"][0]
    assert attempt["state"] == "failed" and attempt["error"] == "ARTICLE_QUALITY_REJECTED"
    trace = (value["core"].settings.root / attempt["quality_trace"]).read_text()
    assert '"approved": false' in trace and '"phase": "rejected"' in trace
    value["review"] = copy.deepcopy(NEW_REVIEW)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_ALREADY_ATTEMPTED"):
        run(value)
    assert len(value["review_calls"]) == len(value["spelling_calls"]) == 1
    assert value["cycle"]._snapshot(record)["metadata"] == value["before"]["metadata"]
    assert_no_delivery(value)


def test_infrastructure_failure_consumes_claim_and_preserves_original_metadata(migration):
    value = migration
    value["failure"] = AgentError("OPENCODE_PROVIDER_UNAVAILABLE", 503)
    with pytest.raises(AgentError, match="OPENCODE_PROVIDER_UNAVAILABLE") as failure:
        run(value)
    assert failure.value.status == 503
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_ALREADY_ATTEMPTED"):
        run(value)
    assert len(value["review_calls"]) == 1 and not value["spelling_calls"]
    assert value["cycle"]._snapshot(value["cycle"]._load()["records"][0]) == value["before"]
    assert_no_delivery(value)


def test_editing_prompt_without_new_rubric_version_cannot_resample_rejected_pair(migration):
    value = migration
    value["review"] = {**OLD_REVIEW, "warnings": []}
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        run(value)
    prompt = value["core"].settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
    prompt.write_text(prompt.read_text() + "\nChanged wording under the same rubric version.\n")
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_ALREADY_ATTEMPTED"):
        run(value)
    assert len(value["review_calls"]) == 1


def test_audit_claim_survives_a_removed_ledger_attempt_and_still_prevents_resampling(migration):
    value = migration
    value["failure"] = AgentError("OPENCODE_PROVIDER_UNAVAILABLE", 503)
    with pytest.raises(AgentError, match="OPENCODE_PROVIDER_UNAVAILABLE"):
        run(value)
    ledger = value["cycle"]._load()
    ledger["records"][0].pop("automatic_migrations")
    value["cycle"]._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_ALREADY_ATTEMPTED"):
        run(value)
    assert len(value["review_calls"]) == 1


def test_final_ledger_failure_blocks_newly_classified_record_until_reconciled(migration, monkeypatch):
    value = migration
    save = value["cycle"]._save

    def fail_ready(ledger):
        if ledger["records"][0]["state"] == "automatic_ready":
            raise OSError("Private storage detail")
        save(ledger)

    monkeypatch.setattr(value["cycle"], "_save", fail_ready)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_INTERRUPTED"):
        run(value)
    record = value["cycle"]._load()["records"][0]
    assert record["state"] == "needs_attention"
    with value["core"].db.session() as session:
        variant = session.get(Variant, record["variant_id"])
        assert automatic_evidence(session, variant) is not None
        with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_ELIGIBLE"):
            eligible(value["core"], session, variant)
    assert len(value["review_calls"]) == 1
    assert_no_delivery(value)


def test_language_tool_grammar_findings_cannot_be_downgraded_to_automatic(migration):
    value = migration
    value["matches"] = [{"message": "A grammar error remains", "offset": 0, "length": 3,
        "rule": {"id": "GRAMMAR_RULE", "issueType": "grammar", "category": {"id": "GRAMMAR"}},
        "type": {"typeName": "Other"}}]
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        run(value)
    assert len(value["review_calls"]) == len(value["spelling_calls"]) == 1
    assert_no_delivery(value)


@pytest.mark.parametrize("block", ["approval", "lease", "remote_edit", "wrong_mode", "wrong_source"])
def test_unsafe_migration_fails_before_new_inference(migration, block):
    value = migration
    if block in {"approval", "lease"}:
        with value["core"].db.session.begin() as session:
            if block == "approval":
                session.add(TargetApproval(variant_id=value["prepared"]["variant_id"], revision=2,
                    snapshot_hash="a" * 64, scheduled_at=value["stamp"][0] + 60, actor="editor"))
            else:
                session.scalar(select(BridgePost)).mutation_owner = uid()
    elif block == "remote_edit":
        value["records"][value["prepared"]["postiz_id"]]["content"] += "\nНесогласованная правка."
    else:
        value["policy"]["approval_mode" if block == "wrong_mode" else "source_id"] = (
            "human" if block == "wrong_mode" else "unrelated-source")
        value["policy_path"].write_text(json.dumps(value["policy"]))
    with pytest.raises(AgentError):
        run(value)
    assert not value["review_calls"] and not value["spelling_calls"]
    assert "automatic_migrations" not in value["cycle"]._load()["records"][0]


@pytest.mark.parametrize("change", ["remote", "policy", "old_trace"])
def test_inputs_changed_during_review_do_not_grant_automatic_eligibility(migration, change):
    value = migration

    def mutate():
        if change == "remote":
            value["records"][value["prepared"]["postiz_id"]]["content"] += "\nПравка другого редактора."
        elif change == "policy":
            value["policy"]["owner_instruction"] += " changed during review"
            value["policy_path"].write_text(json.dumps(value["policy"]))
        else:
            value["old_trace"].write_bytes(value["old_bytes"] + b'\n')

    value["on_review"] = mutate
    with pytest.raises(AgentError):
        run(value)
    record = value["cycle"]._load()["records"][0]
    assert record["state"] == "awaiting_human_review"
    assert value["cycle"]._snapshot(record)["metadata"]["approval_mode"] == "human"
    assert len(value["review_calls"]) == 1
    assert_no_delivery(value)


def test_existing_text_is_not_silently_reformatted_to_the_quality_export(migration):
    value = migration
    remote = value["records"][value["prepared"]["postiz_id"]]
    remote["content"] = remote["content"].removesuffix("\nПеревод и редактура с помощью нейросетей.")
    value["core"].bridge.sync(force=True)
    body = remote["content"]
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_MIGRATION_TEXT_CHANGED"):
        run(value)
    assert remote["content"] == body
    assert len(value["review_calls"]) == 1
    assert_no_delivery(value)


def test_new_number_fails_hard_gate_without_calling_reviewer(migration):
    value = migration
    remote = value["records"][value["prepared"]["postiz_id"]]
    remote["content"] = remote["content"].replace(RU_BODY, RU_BODY + " Число 999.")
    value["core"].bridge.sync(force=True)
    with pytest.raises(AgentError, match="TRANSLATION_NUMBER_CHANGED"):
        run(value)
    assert not value["review_calls"] and not value["spelling_calls"]
    assert_no_delivery(value)
