"""An explicit correction consumes one claim and never approves or schedules."""
import copy
import hashlib
import json

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.automatic_correction import STARTED, correct_staged, recover_staged_spelling
from max_postiz_agent.automatic_policy import automatic_evidence, eligible
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft, DraftRevision, Material
from max_postiz_agent.hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant, VariantRevision
from max_postiz_agent.human_review import CandidateOnlyTranslator
from max_postiz_agent.reddit import text_hash as translation_hash

pytest_plugins = ["test_automatic_policy"]
REASON = "The owner requested correction of a concrete translation error before publication."
CORRECTION = {"title": "Необычный звёздный ветер",
              "body": "Учёные изучили необычную атмосферу и обнаружили звёздный ветер у далёкой звезды."}


@pytest.fixture
def correction_case(automatic_cycle, monkeypatch, request):
    (cycle, core, article, records, writes, stamp, policy), review = automatic_cycle
    if getattr(request, "param", None) == "scientific":
        article.update(title="Sea Surface Temperature Analysis", original=(
            "Sea Surface Temperature data are provided by the Multi-scale Ultra-high Resolution (MUR) Sea Surface Temperature Analysis. "
            "Scientists use this analysis to study the ocean."))
    monkeypatch.setattr("max_postiz_agent.automatic_correction.now", lambda: stamp[0])
    prepared = cycle.prepare(article)
    before = cycle._snapshot(cycle._load()["records"][0])
    original_record = copy.deepcopy(cycle._load()["records"][0])
    old_trace = core.settings.root / original_record["quality_trace"]
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    case = {"cycle": cycle, "core": core, "article": article, "records": records, "writes": writes,
            "stamp": stamp, "policy": policy, "prepared": prepared, "before": before,
            "original_record": original_record, "old_trace": old_trace, "old_bytes": old_trace.read_bytes(),
            "review": review, "review_calls": [], "spelling_calls": [], "updates": [], "matches": [],
            "failure": None, "on_review": None, "on_mutation": None, "after_mutation": None}

    class Reviewer:
        model = "ministral-fixture"

        def __init__(self):
            self.client = self

        def generate_text(self, prompt):
            case["review_calls"].append(json.loads(prompt))
            if case["on_review"]:
                case["on_review"]()
            if case["failure"]:
                raise case["failure"]
            return json.dumps(case["review"]), {}

        def close(self):
            pass

    def quality(record_id=None, review_only=False):
        assert review_only is True and record_id != prepared["id"]

        def spelling(request):
            case["spelling_calls"].append(request)
            return httpx.Response(200, json={"matches": case["matches"]})

        return ArticleQuality(CandidateOnlyTranslator(), Reviewer(), prompt_dir=core.settings.root / "prompts",
            trace=cycle._trace_writer(record_id), review_rubric="meaning-first-v1",
            transport=httpx.MockTransport(spelling))

    def request(method, path, **kwargs):
        assert method == "POST" and path == "/posts"
        payload = kwargs["json"]
        assert payload["type"] == "draft" and payload["republish"] is False
        post = payload["posts"][0]
        item = post["value"][0]
        ident = item["id"]
        assert ident == prepared["postiz_id"] and item["image"] == []
        case["updates"].append(copy.deepcopy(kwargs))
        if case["on_mutation"]:
            case["on_mutation"]()
        lease = core.bridge.mutation([ident], expected_hash=kwargs["expected_cas_hash"], expected_post_id=ident)
        records[ident].update(content=item["content"], group="new-group-from-postiz")
        if case["after_mutation"]:
            case["after_mutation"]()
        core.bridge.mutation([ident], release=lease["owner"])
        return [{"postId": ident}]

    monkeypatch.setattr(cycle, "_quality", quality)
    monkeypatch.setattr(core.postiz, "request", request)
    return case


def run(case, candidate=None, hashed=None):
    return correct_staged(case["cycle"], case["prepared"]["id"], candidate or CORRECTION,
                          hashed or case["prepared"]["content_hash"], REASON)


def no_delivery(case):
    with case["core"].db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert all(target.paused for target in session.scalars(select(Target)))
    assert len(case["writes"]) == len(case["records"]) == 1


def blocked(case, code):
    with pytest.raises(AgentError, match=code):
        run(case)
    assert not case["review_calls"] and not case["updates"]


def test_same_post_correction_has_new_exact_evidence_and_preserves_history(correction_case):
    case = correction_case
    result = run(case)
    assert result["state"] == "automatic_ready" and result["postiz_id"] == case["prepared"]["postiz_id"]
    assert result["title"] == CORRECTION["title"] and result["body"].startswith(CORRECTION["title"] + "\n\n" + CORRECTION["body"])
    assert len(case["updates"]) == len(case["review_calls"]) == len(case["spelling_calls"]) == 1
    assert result["revision"] == case["before"]["revision"] + 3
    assert case["records"][result["postiz_id"]]["state"] == "DRAFT"
    assert case["old_trace"].read_bytes() == case["old_bytes"]
    saved = case["cycle"]._load()["records"][0]
    attempt = saved["automatic_corrections"][0]
    assert attempt["state"] == "completed" and attempt["reason"] == REASON
    assert attempt["previous_trace_hash"] == hashlib.sha256(case["old_bytes"]).hexdigest()
    assert (case["core"].settings.root / attempt["previous_trace_backup"]).read_bytes() == case["old_bytes"]
    backup = json.loads((case["core"].settings.root / attempt["previous_snapshot_backup"]).read_text(encoding="utf-8"))
    assert backup["captured"]["snapshot"] == case["before"] and backup["record"] == case["original_record"]
    assert saved["prepared_hash"] == result["content_hash"] and saved["quality_trace"] != case["original_record"]["quality_trace"]
    with case["core"].db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        history = variant.metadata_json["automatic_correction_history"][0]
        assert history["previous_metadata"] == case["before"]["metadata"]
        assert history["previous_group"] == "group-article" and history["current_group"] == "new-group-from-postiz"
        assert automatic_evidence(session, variant) is not None
        assert eligible(case["core"], session, variant)["approval_mode"] == "automatic"
        assert "translation_model" not in variant.metadata_json
        assert session.scalar(select(Material)).text == case["article"]["original"]
        draft = session.get(Draft, variant.draft_id)
        assert draft.current["body"] == variant.body and draft.revision == variant.source_revision
        assert session.scalar(select(DraftRevision).where(DraftRevision.draft_id == draft.id,
                                                        DraftRevision.revision == draft.revision)).payload["body"] == variant.body
        assert session.scalar(select(VariantRevision).where(VariantRevision.variant_id == variant.id,
            VariantRevision.revision == case["before"]["revision"])).payload == case["before"]
        assert session.scalar(select(Audit).where(Audit.action == STARTED))
    context = case["review_calls"][0]
    assert context["translation_untrusted"] == CORRECTION["title"] + "\n\n" + CORRECTION["body"]
    assert context["source_untrusted"] == case["article"]["title"] + "\n\n" + case["article"]["original"].strip()
    no_delivery(case)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED"):
        run(case)
    assert len(case["review_calls"]) == 1


@pytest.mark.parametrize("change", ["state", "old_slot", "future_slot", "due", "automatic_slot", "hash", "policy"])
def test_ledger_policy_and_hour_block_before_review(correction_case, change):
    case = correction_case
    if change == "policy":
        path = case["core"].settings.root / "data/hourly-article-policy.local.json"
        policy = json.loads(path.read_text())
        policy["rights_evidence"] = "different_permission"
        path.write_text(json.dumps(policy))
    elif change == "hash":
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_HASH_MISMATCH"):
            run(case, hashed="0" * 64)
        assert not case["review_calls"] and not case["updates"]
        return
    else:
        ledger = case["cycle"]._load()
        record = ledger["records"][0]
        key, value = {"state": ("state", "pending"), "old_slot": ("slot", record["slot"] - 1),
                      "future_slot": ("slot", record["slot"] + 1), "due": ("due_at", case["stamp"][0] + 120),
                      "automatic_slot": ("automatic_slot", record["slot"])}[change]
        record[key] = value
        case["cycle"]._save(ledger)
    blocked(case, "ARTICLE_AUTOMATIC_CORRECTION_NOT_SAFE")


@pytest.mark.parametrize("change", ["unpaused", "owner", "orphan_lease", "cancelled_dispatch", "revoked_approval",
                                   "remote_unknown", "remote_queue", "remote_edit", "source", "config", "binding", "trace"])
def test_local_and_remote_safety_guards_never_mutate(correction_case, change):
    case = correction_case
    core = case["core"]
    with core.db.session.begin() as session:
        variant = session.get(Variant, case["prepared"]["variant_id"])
        link = session.get(BridgePost, case["prepared"]["postiz_id"])
        if change == "unpaused":
            session.get(Target, "max").paused = False
        elif change == "owner":
            link.mutation_owner = "existing-owner"
        elif change == "orphan_lease":
            link.mutation_until = 1
        elif change in {"cancelled_dispatch", "revoked_approval"}:
            approval = TargetApproval(variant_id=variant.id, revision=variant.revision, snapshot_hash="a" * 64,
                                      scheduled_at=case["stamp"][0] + 120, actor="old", revoked=True)
            session.add(approval)
            session.flush()
            if change == "cancelled_dispatch":
                session.add(Dispatch(variant_id=variant.id, approval_id=approval.id, revision=variant.revision,
                    snapshot_hash="a" * 64, snapshot=case["before"], due_at=case["stamp"][0], state="cancelled"))
        elif change == "remote_unknown":
            session.get(RemoteDraft, variant.id).state = "unknown"
        elif change == "source":
            session.scalar(select(Material)).text += " A changed scientific fact."
        elif change == "config":
            variant.config_hash = "0" * 64
        elif change == "binding":
            session.get(Target, "max").binding_hash = "0" * 64
    if change == "remote_queue":
        case["records"][case["prepared"]["postiz_id"]]["state"] = "QUEUE"
    elif change == "remote_edit":
        case["records"][case["prepared"]["postiz_id"]]["content"] += " Другая правка."
    elif change == "trace":
        case["old_trace"].write_text("Changed trace", encoding="utf-8")
    with pytest.raises(AgentError):
        run(case)
    assert not case["review_calls"] and not case["updates"]


def test_unchanged_candidate_is_not_reviewed_again(correction_case):
    case = correction_case
    old = {"title": case["before"]["title"], "body": "Учёные изучили необычные процессы в атмосфере далёкой звезды."}
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_UNCHANGED"):
        run(case, {key: "  " + value + "\n" for key, value in old.items()})
    assert not case["review_calls"] and not case["updates"]
    with case["core"].db.session() as session:
        assert not session.scalar(select(Audit).where(Audit.action == STARTED))


@pytest.mark.parametrize("failure", ["negative", "infrastructure", "spelling"])
def test_failure_preserves_original_verdict_consumes_claim_and_blocks_publication(correction_case, failure):
    case = correction_case
    if failure == "negative":
        case["review"].update(approved=False, issues=["The candidate changes the scientific meaning."], warnings=[])
    elif failure == "infrastructure":
        case["failure"] = AgentError("OPENCODE_PROVIDER_UNAVAILABLE", 503)
    else:
        case["matches"] = [{"offset": 0, "length": 10, "message": "A concrete spelling error.",
                            "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}}]
    with pytest.raises(AgentError):
        run(case)
    saved = case["cycle"]._load()["records"][0]
    assert saved["state"] == "needs_attention" and saved["correction_blocked"] is True
    assert case["old_trace"].read_bytes() == case["old_bytes"] and not case["updates"]
    with case["core"].db.session() as session:
        variant = session.get(Variant, saved["variant_id"])
        assert variant.metadata_json["machine_review_evidence"] == case["before"]["metadata"]["machine_review_evidence"]
        assert variant.metadata_json["approval_mode"] == "automatic_correction_pending"
        with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_ELIGIBLE"):
            eligible(case["core"], session, variant)
    if failure == "negative":
        trace = (case["core"].settings.root / saved["automatic_corrections"][0]["quality_trace"]).read_text(encoding="utf-8")
        assert '"approved": false' in trace and "changes the scientific meaning" in trace
    ledger = case["cycle"]._load()
    ledger["records"][0].pop("automatic_corrections")
    ledger["records"][0]["state"] = "automatic_ready"
    case["cycle"]._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED"):
        run(case)
    assert len(case["review_calls"]) == 1 and not case["updates"]
    no_delivery(case)


def test_unknown_mutation_is_not_retried_or_unlocked(correction_case):
    case = correction_case

    def unknown():
        raise AgentError("POSTIZ_UNKNOWN", 503)

    case["after_mutation"] = unknown
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        run(case)
    assert len(case["updates"]) == len(case["review_calls"]) == 1
    with case["core"].db.session() as session:
        link = session.get(BridgePost, case["prepared"]["postiz_id"])
        assert link.mutation_owner and link.mutation_until > 0
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED"):
        run(case)
    assert len(case["updates"]) == 1
    no_delivery(case)


@pytest.mark.parametrize("change", ["variant", "source", "policy"])
def test_concurrent_input_changes_during_review_block_remote_write(correction_case, change):
    case = correction_case

    def changed():
        if change == "policy":
            path = case["core"].settings.root / "data/hourly-article-policy.local.json"
            policy = json.loads(path.read_text())
            policy["owner_instruction"] += " Changed."
            path.write_text(json.dumps(policy))
        else:
            with case["core"].db.session.begin() as session:
                if change == "variant":
                    session.get(Variant, case["prepared"]["variant_id"]).body += " Новая редактура."
                else:
                    session.scalar(select(Material)).text += " Changed source."

    case["on_review"] = changed
    with pytest.raises(AgentError):
        run(case)
    assert len(case["review_calls"]) == 1 and not case["updates"]
    no_delivery(case)


def test_native_compare_and_swap_preserves_concurrent_postiz_edit(correction_case):
    case = correction_case

    def concurrent_edit():
        case["records"][case["prepared"]["postiz_id"]]["content"] += " Независимая правка редактора."

    case["on_mutation"] = concurrent_edit
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_CHANGED"):
        run(case)
    assert len(case["updates"]) == 1
    assert case["records"][case["prepared"]["postiz_id"]]["content"].endswith("Независимая правка редактора.")
    assert case["cycle"]._load()["records"][0]["state"] == "needs_attention"
    no_delivery(case)


def test_claim_commit_blocks_old_passed_text_if_first_ledger_save_crashes(correction_case, monkeypatch):
    case = correction_case

    def fail_save(_):
        raise OSError("Private storage unavailable")

    monkeypatch.setattr(case["cycle"], "_save", fail_save)
    with pytest.raises(OSError):
        run(case)
    record = case["cycle"]._load()["records"][0]
    assert record["state"] == "automatic_ready" and not record.get("automatic_corrections")
    with case["core"].db.session() as session:
        variant = session.get(Variant, record["variant_id"])
        assert session.scalar(select(Audit).where(Audit.action == STARTED))
        assert variant.metadata_json["approval_mode"] == "automatic_correction_pending"
        assert variant.metadata_json["machine_review_evidence"] == case["before"]["metadata"]["machine_review_evidence"]
        current_hash = fingerprint(case["core"].snapshot(variant, session.get(Target, variant.target_id)))
        with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_ELIGIBLE"):
            eligible(case["core"], session, variant)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_(NOT_ELIGIBLE|NOT_READY)"):
        case["cycle"].publish(record["id"], current_hash)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED"):
        run(case)
    assert not case["review_calls"] and not case["updates"]
    no_delivery(case)


def test_null_optional_ledger_mode_keeps_authoritative_metadata_requirement(correction_case):
    case = correction_case
    ledger = case["cycle"]._load()
    ledger["records"][0]["approval_mode"] = None
    case["cycle"]._save(ledger)
    assert run(case)["state"] == "automatic_ready"
    no_delivery(case)


@pytest.mark.parametrize("change", ["unpaused_target", "sending", "unknown", "orphan_lease"])
def test_unrelated_target_activity_also_blocks_correction(correction_case, change):
    case = correction_case
    with case["core"].db.session.begin() as session:
        if change == "unpaused_target":
            session.get(Target, "pikabu").paused = False
        else:
            original = session.get(Variant, case["prepared"]["variant_id"])
            sibling = Variant(draft_id=original.draft_id, target_id="pikabu", title="Другой текст", body="Другой текст.",
                source_revision=original.source_revision, config_hash=original.config_hash,
                binding_hash=session.get(Target, "pikabu").binding_hash)
            session.add(sibling)
            session.flush()
            if change == "orphan_lease":
                session.add(BridgePost(post_id="another-post", integration_id="other-integration", group_id="another-group",
                    target_id="pikabu", variant_id=sibling.id, remote_hash="0" * 64, remote_state="DRAFT",
                    due_at=case["stamp"][0], mutation_until=1))
            else:
                approval = TargetApproval(variant_id=sibling.id, revision=1, snapshot_hash="a" * 64,
                                          scheduled_at=case["stamp"][0], actor="other")
                session.add(approval)
                session.flush()
                session.add(Dispatch(variant_id=sibling.id, approval_id=approval.id, revision=1,
                    snapshot_hash="a" * 64, snapshot={}, due_at=case["stamp"][0], state=change))
    blocked(case, "ARTICLE_AUTOMATIC_CORRECTION_NOT_QUIET")


def test_duplicate_corrected_translation_blocks_before_remote_edit(correction_case):
    case = correction_case
    ledger = case["cycle"]._load()
    ledger["records"].append({"id": "other-record", "state": "published", "slot": ledger["records"][0]["slot"] - 1,
        "translation_hash": translation_hash(CORRECTION["title"] + "\n\n" + CORRECTION["body"])})
    case["cycle"]._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_TRANSLATION"):
        run(case)
    assert len(case["review_calls"]) == 1 and not case["updates"]
    no_delivery(case)


@pytest.mark.parametrize("change", ["content", "date", "state", "settings"])
def test_remote_postcheck_mismatch_never_installs_new_machine_proof(correction_case, change):
    case = correction_case

    def changed():
        remote = case["records"][case["prepared"]["postiz_id"]]
        key, value = {"content": ("content", "Другая правка после изменения."),
                      "date": ("publishDate", "2026-10-22T00:00:00+00:00"),
                      "state": ("state", "QUEUE"), "settings": ("settings", "{}")}[change]
        remote[key] = value

    case["after_mutation"] = changed
    with pytest.raises(AgentError, match="ARTICLE_REMOTE_CHANGED"):
        run(case)
    saved = case["cycle"]._load()["records"][0]
    assert saved["state"] == "needs_attention" and saved["correction_blocked"] is True
    assert len(case["updates"]) == 1
    with case["core"].db.session() as session:
        variant = session.get(Variant, saved["variant_id"])
        assert variant.metadata_json["approval_mode"] == "automatic_correction_pending"
        assert variant.metadata_json["machine_review_evidence"] == case["before"]["metadata"]["machine_review_evidence"]
    no_delivery(case)


def test_final_ledger_failure_does_not_reenable_delivery(correction_case, monkeypatch):
    case = correction_case
    save = case["cycle"]._save

    def fail_ready(ledger):
        if ledger["records"][0]["state"] == "automatic_ready":
            raise OSError("Private storage unavailable")
        save(ledger)

    monkeypatch.setattr(case["cycle"], "_save", fail_ready)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_INTERRUPTED"):
        run(case)
    saved = case["cycle"]._load()["records"][0]
    assert saved["state"] == "needs_attention" and saved["correction_blocked"] is True
    with case["core"].db.session() as session:
        variant = session.get(Variant, saved["variant_id"])
        assert automatic_evidence(session, variant) is not None
        with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_ELIGIBLE"):
            eligible(case["core"], session, variant)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED"):
        run(case)
    assert len(case["updates"]) == len(case["review_calls"]) == 1
    no_delivery(case)


SCIENTIFIC_CORRECTION = {"title": "Анализ температуры поверхности моря",
    "body": "Учёные используют данные многомасштабного анализа температуры поверхности моря сверхвысокого разрешения (MUR) для изучения океана."}


@pytest.fixture
def spelling_case(correction_case, monkeypatch):
    from max_postiz_agent.article_quality import rubric_spelling
    case = correction_case
    text = SCIENTIFIC_CORRECTION["title"] + "\n\n" + SCIENTIFIC_CORRECTION["body"]
    offset = len(text[:text.index("многомасштабного")].encode("utf-16-le")) // 2
    match = {"offset": offset, "length": len("многомасштабного"), "type": {"typeName": "UnknownWord"},
             "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}, "message": "Unknown scientific term."}
    case["matches"] = [match]
    # Reproduce the preceding release's exact sole LT false positive, then
    # restore today's source-bound scientific adjudication for recovery.
    monkeypatch.setattr("max_postiz_agent.article_quality.rubric_spelling",
                        lambda *args: ([], [match]) if args[2] else rubric_spelling(*args))
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        run(case, SCIENTIFIC_CORRECTION)
    monkeypatch.setattr("max_postiz_agent.article_quality.rubric_spelling", rubric_spelling)
    saved = case["cycle"]._load()["records"][0]
    case["failed_record"] = copy.deepcopy(saved)
    case["failed_attempt"] = copy.deepcopy(saved["automatic_corrections"][0])
    case["failed_path"] = case["core"].settings.root / case["failed_attempt"]["quality_trace"]
    case["failed_bytes"] = case["failed_path"].read_bytes()
    case["trace_sha"] = hashlib.sha256(case["failed_bytes"]).hexdigest()
    case["pending_hash"] = fingerprint(case["cycle"]._snapshot(saved))
    case["review_calls"].clear()
    case["spelling_calls"].clear()
    return case


def recover(case, *, candidate=None, hashed=None, trace_hash=None):
    return recover_staged_spelling(case["cycle"], case["prepared"]["id"], candidate or SCIENTIFIC_CORRECTION,
        hashed or case["pending_hash"], trace_hash or case["trace_sha"], REASON)


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
def test_scientific_recovery_reuses_saved_review_once_and_preserves_failed_attempt(spelling_case):
    case = spelling_case
    result = recover(case)
    saved = case["cycle"]._load()["records"][0]
    assert result["state"] == "automatic_ready" and result["postiz_id"] == case["prepared"]["postiz_id"]
    assert saved["automatic_corrections"] == case["failed_record"]["automatic_corrections"]
    assert saved["automatic_spelling_recoveries"][0]["state"] == "completed"
    assert case["failed_path"].read_bytes() == case["failed_bytes"] and case["old_trace"].read_bytes() == case["old_bytes"]
    assert not case["review_calls"] and len(case["spelling_calls"]) == len(case["updates"]) == 1
    with case["core"].db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert automatic_evidence(session, variant) is not None
        assert variant.metadata_json["quality"] == "saved_ministral_fresh_gates_languagetool_scientific_recovery"
        assert variant.metadata_json["machine_review_evidence"]["spelling_adjudicated"]
        assert session.scalar(select(Audit).where(Audit.action == STARTED))
        assert session.scalar(select(Audit).where(Audit.action == "hourly_staged_spelling_recovery_started"))
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED"):
        recover(case)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED"):
        run(case, SCIENTIFIC_CORRECTION)
    no_delivery(case)


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
@pytest.mark.parametrize("change", ["candidate", "old_hash", "trace_hash", "hour", "mutation", "failure", "backup"])
def test_recovery_preconditions_are_exact_and_never_call_models(spelling_case, change):
    case = spelling_case
    if change in {"candidate", "old_hash", "trace_hash"}:
        kwargs = {"candidate": {"candidate": {**SCIENTIFIC_CORRECTION, "body": SCIENTIFIC_CORRECTION["body"] + " Другая версия."}},
                  "old_hash": {"hashed": case["prepared"]["content_hash"]}, "trace_hash": {"trace_hash": "0" * 64}}[change]
    else:
        kwargs = {}
        ledger = case["cycle"]._load()
        attempt = ledger["records"][0]["automatic_corrections"][0]
        if change == "hour":
            case["stamp"][0] += 3600
        elif change == "mutation":
            attempt["mutation_started_at"] = case["stamp"][0]
        elif change == "failure":
            attempt["error"] = "POSTIZ_UNKNOWN"
        else:
            path = case["core"].settings.root / attempt["previous_snapshot_backup"]
            before = json.loads(path.read_text(encoding="utf-8"))
            before["captured"]["snapshot"]["body"] += " Unrelated edit."
            path.write_text(json.dumps(before), encoding="utf-8")
        case["cycle"]._save(ledger)
    with pytest.raises(AgentError):
        recover(case, **kwargs)
    assert not case["review_calls"] and not case["spelling_calls"] and not case["updates"]


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
@pytest.mark.parametrize("change", ["candidate", "source", "prompt", "raw_negative", "parsed", "lt_other", "gate_missing", "extra_phase"])
def test_trace_shape_and_positive_review_cannot_be_substituted(spelling_case, change):
    case = spelling_case
    events = [json.loads(line) for line in case["failed_bytes"].decode("utf-8").splitlines()]
    if change == "candidate":
        events[0]["candidate"]["body"] += " Changed."
    elif change == "source":
        events[0]["source_untrusted"]["body"] += " Changed."
    elif change == "prompt":
        events[2]["context"]["instruction"] += " Changed."
    elif change == "raw_negative":
        events[3]["raw"] = json.dumps({"approved": False, "topic": "science", "issues": ["A changed fact."], "warnings": []})
    elif change == "parsed":
        events[4]["review"]["warnings"] = []
    elif change == "lt_other":
        events[6]["matches"][0]["rule"]["id"] = "OTHER_GRAMMAR_RULE"
    elif change == "gate_missing":
        events.pop(1)
    else:
        events.append({**events[-1], "phase": "accepted"})
    case["failed_path"].write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    changed_hash = hashlib.sha256(case["failed_path"].read_bytes()).hexdigest()
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_TRACE_INVALID"):
        recover(case, trace_hash=changed_hash)
    assert not case["review_calls"] and not case["spelling_calls"] and not case["updates"]


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
def test_recovery_fresh_lt_failure_consumes_recovery_claim_without_model(spelling_case):
    case = spelling_case
    case["matches"] = [{"offset": 0, "length": 4, "message": "A real error.",
                        "rule": {"id": "OTHER_GRAMMAR_RULE", "issueType": "grammar"}}]
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        recover(case)
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED"):
        recover(case)
    saved = case["cycle"]._load()["records"][0]
    assert saved["state"] == "needs_attention" and saved["automatic_corrections"] == case["failed_record"]["automatic_corrections"]
    assert not case["review_calls"] and len(case["spelling_calls"]) == 1 and not case["updates"]
    no_delivery(case)


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
def test_recovery_unknown_mutation_keeps_both_claims_and_native_lease(spelling_case):
    case = spelling_case

    def unknown():
        raise AgentError("POSTIZ_UNKNOWN", 503)

    case["after_mutation"] = unknown
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        recover(case)
    with pytest.raises(AgentError, match="ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED"):
        recover(case)
    with case["core"].db.session() as session:
        assert session.get(BridgePost, case["prepared"]["postiz_id"]).mutation_owner
    assert not case["review_calls"] and len(case["updates"]) == 1
    no_delivery(case)


@pytest.mark.parametrize("correction_case", ["scientific"], indirect=True)
def test_saved_failed_trace_tampering_during_fresh_lt_blocks_before_post(spelling_case, monkeypatch):
    case = spelling_case
    original_quality = case["cycle"]._quality

    def quality(*args, **kwargs):
        result = original_quality(*args, **kwargs)
        check = result._spelling_matches

        def changed(text):
            value = check(text)
            case["failed_path"].write_text("Changed failed trace", encoding="utf-8")
            return value

        result._spelling_matches = changed
        return result

    monkeypatch.setattr(case["cycle"], "_quality", quality)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_CORRECTION_INPUT_CHANGED"):
        recover(case)
    assert not case["review_calls"] and len(case["spelling_calls"]) == 1 and not case["updates"]
    no_delivery(case)
