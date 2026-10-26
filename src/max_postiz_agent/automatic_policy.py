"""Explicit automatic approvals; never impersonates a human editor."""
import copy
import hashlib
import math
import re
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters import extract_object
from .article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT, reference_urls
from .config import AgentError, fingerprint, inside
from .db import Audit, Material, now
from .hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval
from .human_review import candidate_body, machine_current, source_data, text_hash

POLICY_ACTOR = "hourly_article_policy"


def _validate(evidence, title, body, source):
    from .article_quality import (FRESH_SPACETIME_SPELLING_CONTRACT, MAX_REVIEW_ISSUES, MEANING_FIRST_REVIEW_RUBRIC, TOPICS,
                                  normalize_rubric_review, protocol_recovery_provenance,
                                  review_parser_contract, rubric_spelling, rubric_translation_gate)
    required = {"review_raw", "review", "spelling_matches", "spelling_adjudicated", "spelling_unresolved",
                "hard_gates_passed", "text_hash", "source_hash", "review_rubric", "review_prompt_hash", "trace_hash"}
    optional = {"parser_contract", "protocol_recovery", "fresh_spelling_contract", "reference_check"}
    if (not isinstance(evidence, dict) or not required <= set(evidence) <= required | optional
            or evidence["review_rubric"] != MEANING_FIRST_REVIEW_RUBRIC
            or evidence["hard_gates_passed"] is not True
            or evidence["text_hash"] != text_hash(title, body)
            or evidence["source_hash"] != fingerprint({**source, "title": source["title"].strip(),
                                                       "body": source["body"].strip()})
            or any(not isinstance(evidence[key], str) or not re.fullmatch(r"[0-9a-f]{64}", evidence[key])
                   for key in ("review_prompt_hash", "trace_hash"))):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    if "fresh_spelling_contract" in evidence and (
            evidence["fresh_spelling_contract"] != FRESH_SPACETIME_SPELLING_CONTRACT
            or "protocol_recovery" in evidence):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    rubric = evidence["review_rubric"]
    review = normalize_rubric_review(evidence["review"], rubric)
    if (normalize_rubric_review(extract_object(evidence["review_raw"]), rubric) != review
            or review["approved"] is not True or review["issues"] or review["topic"] not in TOPICS):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    if "parser_contract" in evidence:
        if (not isinstance(evidence["parser_contract"], dict)
                or fingerprint(evidence["parser_contract"]) != fingerprint(review_parser_contract(evidence["review_raw"]))):
            raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    elif len(review["warnings"]) > MAX_REVIEW_ISSUES:
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    if "protocol_recovery" in evidence and (
            "parser_contract" not in evidence
            or not isinstance(evidence["protocol_recovery"], dict)
            or fingerprint(evidence["protocol_recovery"]) != fingerprint(protocol_recovery_provenance(
                evidence["review_raw"], evidence["review_prompt_hash"]))):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    candidate = title + "\n\n" + candidate_body(SimpleNamespace(title=title, body=body), source)
    original = source["title"] + "\n\n" + source["body"]
    reference_contract = LEGACY_REFERENCE_CONTRACT
    if "reference_check" in evidence:
        reference_contract = REFERENCE_CONTRACT
        expected = {"contract": reference_contract,
                    "source_urls": sorted(reference_urls(original, contract=reference_contract)),
                    "candidate_urls": sorted(reference_urls(candidate, contract=reference_contract))}
        if evidence["reference_check"] != expected:
            raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    # Сохранённое доказательство воспроизводится своим историческим валидатором.
    # Перед будущей отправкой eligibility отдельно требует строгую новую проверку.
    rubric_translation_gate(original, candidate, rubric, reference_contract=reference_contract)
    if len(body) > 3500 or source["url"] not in body:
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    matches = evidence["spelling_matches"]
    if not isinstance(matches, list) or any(not isinstance(value, dict) for value in matches):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    adjudicated, unresolved = rubric_spelling(original, candidate, matches, rubric,
        allow_spacetime=evidence.get("fresh_spelling_contract") == FRESH_SPACETIME_SPELLING_CONTRACT)
    if (unresolved or evidence["spelling_adjudicated"] != adjudicated
            or evidence["spelling_unresolved"] != unresolved):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    return review


def machine_metadata(prepared, source, trace_path):
    evidence = copy.deepcopy(prepared.get("machine_review_evidence"))
    if not isinstance(evidence, dict) or prepared.get("machine_review_status") != "passed":
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)
    hashed = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    if evidence.get("trace_hash", hashed) != hashed:
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    evidence["trace_hash"] = hashed
    review = _validate(evidence, prepared["title"], prepared["body"], source)
    if (prepared.get("review_rubric") != evidence["review_rubric"]
            or prepared.get("review_prompt_hash") != evidence["review_prompt_hash"]
            or evidence["review_prompt_hash"] != hashlib.sha256((trace_path.parents[2] /
                "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt").read_text(encoding="utf-8").encode("utf-8")).hexdigest()):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    reference_contract = evidence.get("reference_check", {}).get("contract", LEGACY_REFERENCE_CONTRACT)
    if prepared.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract:
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    return {"machine_review_status": "passed", "machine_review_issues": [],
            "machine_review_warnings": review["warnings"], "machine_review_evidence": evidence,
            "machine_review_evidence_hash": fingerprint(evidence), "review_rubric": evidence["review_rubric"],
            "review_prompt_hash": evidence["review_prompt_hash"],
            **({"reference_contract": reference_contract}
               if reference_contract != LEGACY_REFERENCE_CONTRACT else {})}


def automatic_evidence(session, variant):
    if variant.metadata_json.get("machine_review_status") != "passed":
        return None
    try:
        if not machine_current(session, variant):
            return None
        evidence = variant.metadata_json.get("machine_review_evidence")
        review = _validate(evidence, variant.title, variant.body, source_data(session, variant))
        hashed = fingerprint(evidence)
        if (hashed != variant.metadata_json.get("machine_review_evidence_hash")
                or variant.metadata_json.get("review_rubric") != evidence["review_rubric"]
                or variant.metadata_json.get("review_prompt_hash") != evidence["review_prompt_hash"]
                or variant.metadata_json.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
                != evidence.get("reference_check", {}).get("contract", LEGACY_REFERENCE_CONTRACT)):
            return None
        return {"evidence_hash": hashed, "review": copy.deepcopy(review), "warnings": review["warnings"]}
    except (AgentError, ValueError, TypeError, KeyError):
        return None


def eligible(core, session, variant):
    from .article_cycle import HourlyArticleCycle
    cycle = HourlyArticleCycle(core)
    record = next((value for value in cycle._load()["records"] if value.get("variant_id") == variant.id), None)
    if record is None:
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    policy = cycle._policy_for_record(record)
    return _eligible_record(cycle, session, variant, policy, record)


def _reference_binding(session, variant, record):
    """Новый контракт нельзя удалить из ledger/evidence и выдать за старую проверку."""
    audits = list(session.scalars(select(Audit).where(
        Audit.entity == record["id"], Audit.action == "hourly_article_prepare_attempt")))
    evidence = variant.metadata_json.get("machine_review_evidence", {})
    check = evidence.get("reference_check", {}) if isinstance(evidence, dict) else {}
    attempts = record.get("preparation_attempts", [])
    if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)
    markers = [record.get("reference_contract"), variant.metadata_json.get("reference_contract"),
               check.get("contract") if isinstance(check, dict) else None]
    markers += [item.get("reference_contract") for item in attempts if isinstance(item, dict)]
    markers += [item.details.get("reference_contract") for item in audits if isinstance(item.details, dict)]
    if not any(marker is not None for marker in markers):
        return  # Исторический v1 остаётся историческим; запись не переписывается.
    if not attempts or any(type(item.get("number")) is not int or item["number"] != number
                           for number, item in enumerate(attempts, 1)):
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)
    last = attempts[-1]
    matching = [item for item in audits if isinstance(item.details, dict)
                and type(item.details.get("attempt")) is int
                and item.details["attempt"] == last["number"]]
    if (record.get("reference_contract") != REFERENCE_CONTRACT
            or last.get("reference_contract") != REFERENCE_CONTRACT
            or variant.metadata_json.get("reference_contract") != REFERENCE_CONTRACT
            or not isinstance(check, dict) or check.get("contract") != REFERENCE_CONTRACT
            or len(matching) != 1 or matching[0].details.get("reference_contract") != REFERENCE_CONTRACT):
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)


def _eligible_record(cycle, session, variant, policy, record, *, reserve_unassigned=False):
    core = cycle.core
    if not isinstance(record, dict):
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    reserve_binding = session.get(Audit, str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record["id"])))
    if (record.get("reserve_contract") is not None or variant.metadata_json.get("reserve_contract") is not None
            or reserve_binding is not None):
        cycle._reserve_record(record, session=session)
        if (variant.metadata_json.get("reserve_contract") != record.get("reserve_contract")
                or variant.metadata_json.get("reserve_binding_hash") != record.get("reserve_binding_hash")):
            raise AgentError("ARTICLE_RESERVE_BINDING_INVALID", 409)
        if not reserve_unassigned:
            from .article_reserve import ArticleReserveStore
            assigned = ArticleReserveStore(core).require_assignment(record["id"], session=session)
            if (assigned["text_hash"] != record.get("reserve_prepared_hash")
                    or assigned["evidence_hash"] != variant.metadata_json.get("machine_review_evidence_hash")):
                raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_CHANGED", 409)
    if (policy.get("approval_mode") != "automatic"
            or variant.metadata_json.get("approval_mode") != "automatic"
            or variant.metadata_json.get("origin") != "hourly_article_test"
            or variant.target_id != policy["target_id"]
            or variant.metadata_json.get("rights_evidence") != policy["rights_evidence"]
            or variant.metadata_json.get("policy_hash") != fingerprint(policy)):
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    cycle._source_target(policy)
    if variant.media:
        raise AgentError("ARTICLE_MEDIA_NOT_ALLOWED", 409)
    if not record or record["state"] not in {"automatic_ready", "automatic_pending_approval", "pending", "sending", "awaiting_postiz"}:
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    if (record.get("source_id") != policy["source_id"]
            or record.get("target_id") != policy["target_id"]
            or record.get("policy_hash") != fingerprint(policy)):
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    from .article_repair_cycle import AutomaticArticleRepair
    AutomaticArticleRepair(cycle).assert_complete(record, session)
    material = session.get(Material, variant.sources[0].get("id")) if len(variant.sources) == 1 else None
    if (not material or material.source_id != policy["source_id"]
            or material.url != record.get("url")
            or material.published_at != record.get("source_published_at")
            or material.published_at is None
            or material.published_at < now() - policy["max_source_age_hours"] * 3600):
        raise AgentError("STALE_SOURCE", 409)
    if automatic_evidence(session, variant) is None:
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)
    _reference_binding(session, variant, record)
    from .article_quality import rubric_translation_gate
    source = source_data(session, variant)
    # Даже старый прошедший DRAFT не может отправить повреждённый literal URL.
    rubric_translation_gate(source["title"] + "\n\n" + source["body"],
        variant.title + "\n\n" + candidate_body(variant, source), "meaning-first-v1",
        reference_contract=REFERENCE_CONTRACT)
    prompt_hash = hashlib.sha256((core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt")
                                .read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    if variant.metadata_json.get("review_prompt_hash") != prompt_hash:
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)
    return policy


def check_approval(core, session, variant, target, dispatch, approval, evidence, policy):
    rows = list(session.scalars(select(Audit).where(Audit.entity == approval.id,
                                                   Audit.action == "hourly_article_policy_review")))
    expected = {"approval_id": approval.id, "variant_id": variant.id, "revision": variant.revision,
                "content_hash": fingerprint(core.snapshot(variant, target)), "snapshot_hash": dispatch.snapshot_hash,
                "due_at": dispatch.due_at, "target_id": target.id, "destination": target.destination,
                "evidence_hash": evidence["evidence_hash"], "policy_hash": fingerprint(policy),
                "review_rubric": variant.metadata_json["review_rubric"],
                "approval_mode": "automatic"}
    if (len(rows) != 1 or rows[0].actor != POLICY_ACTOR or not isinstance(rows[0].details, dict)
            or fingerprint({key: rows[0].details.get(key) for key in expected}) != fingerprint(expected)):
        raise AgentError("ARTICLE_POLICY_APPROVAL_STALE", 409)


def publish(cycle, record, ledger, reviewed_hash, due_at):
    from .server_state import require_running
    require_running(cycle.core)
    core, policy = cycle.core, cycle._policy_for_record(record)
    from .article_repair_cycle import AutomaticArticleRepair
    with core.db.session() as session:
        AutomaticArticleRepair(cycle).assert_complete(record, session)
    if policy.get("approval_mode") != "automatic":
        raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
    if record["state"] not in {"prepared", "automatic_ready"}:
        raise AgentError("ARTICLE_AUTOMATIC_NOT_READY", 409)
    slot = int(now() // 3600)
    reserved = record.get("reserve_contract") == "article-reserve-v1"
    assigned = None
    if reserved:
        from .article_reserve import ArticleReserveStore
        cycle._reserve_record(record)
        assigned = ArticleReserveStore(core).require_assignment(record["id"])
    # A completed, unpublished draft may cross an hour boundary while models
    # or Postiz finish. Reserve this hour for that one draft, instead of trying
    # to catch up missed source slots or replay a late dispatch.
    if record["slot"] > slot:
        raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
    if not reserved and any(item is not record and (item["slot"] == slot or item.get("automatic_slot") == slot)
           for item in ledger["records"]):
        raise AgentError("ARTICLE_SLOT_ALREADY_CLAIMED", 409)
    cycle._clear_outstanding(record["target_id"])
    earliest = cycle._cadence(policy, now(), check_gap=False)
    due = float(max(now() + 120, earliest) if due_at is None else due_at)
    if assigned is not None and due != assigned["due_at"]:
        raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_CHANGED", 409)
    if not now() + 30 <= due <= now() + 3700:
        raise AgentError("ARTICLE_AUTOMATIC_DATE_INVALID", 409)
    cycle._cadence(policy, due)
    with core.db.session() as session:
        require_running(core, session, lock=True)
        variant = core._variant(session, record["variant_id"], True)
        target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
        eligible(core, session, variant)
        before = copy.deepcopy(core.snapshot(variant, target))
        if reviewed_hash != fingerprint(before) or record["policy_hash"] != fingerprint(policy):
            raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
        # No prior approved or uncertain outcome may be repurposed.
        if (session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
                or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
                or automatic_evidence(session, variant) is None):
            raise AgentError("ARTICLE_AUTOMATIC_NOT_READY", 409)
        core._check_schedule_limits(session, variant, target, due, automatic_policy=policy)
        link = session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
        post_id = link.post_id if link else None
    record.update(state="automatic_scheduling", automatic_slot=slot, due_at=due)
    cycle._save(ledger)
    try:
        if post_id:
            # Postiz's MaxBrowserMutationGuard owns the one end-to-end lease.
            # Taking another lease here makes its nested /mutation/start fail.
            core.postiz.reschedule_existing(post_id, before["body"], due)
            # The guard releases asynchronously on HTTP finish. Only observe;
            # never expire or unlock an uncertain mutation to obtain permission.
            deadline = time.monotonic() + 5
            while True:
                with core.db.session() as session:
                    link = session.get(BridgePost, post_id)
                    if not link:
                        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
                    if not link.mutation_owner:
                        break
                if time.monotonic() >= deadline:
                    raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
                time.sleep(0.05)
            core.bridge.sync(force=True)
        else:
            post_id = core.stage_remote(record["variant_id"], due_at=due)
        fresh = cycle._snapshot(record)
        if (cycle._reviewed_fields(fresh) != cycle._reviewed_fields(before)
                or fresh["revision"] != before["revision"] + 1
                or fresh["metadata"].get("postiz_state") != "QUEUE"
                or abs(fresh["metadata"].get("postiz_due_at", 0) - due) > 0.001):
            raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
        record.update(state="automatic_pending_approval", postiz_id=post_id, prepared_hash=fingerprint(fresh))
        cycle._save(ledger)
        dispatch_id = core.approve_hourly_policy(record["variant_id"], fresh["revision"], fingerprint(fresh), due)
        record.update(state="pending", dispatch_id=dispatch_id)
        cycle._save(ledger)
        return cycle._view(record)
    except Exception as exc:
        # An ambiguous remote mutation retains its lease and is never retried.
        record.update(state="needs_attention", error=exc.code if isinstance(exc, AgentError) else "ARTICLE_APPROVAL_INTERRUPTED")
        cycle._save(ledger)
        raise


def _reconcile_capture(cycle, session, record, policy, reviewed_hash, due_at):
    from .hub import ACTIVE
    from .reddit import text_hash as original_hash
    core = cycle.core
    variant = core._variant(session, record["variant_id"], True)
    target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
    if (not target or not target.paused or variant.state != "review_pending" or variant.media
            or record.get("policy_hash") != fingerprint(policy)
            or record.get("target_id") != policy["target_id"]
            or record.get("source_id") != policy["source_id"]
            or variant.metadata_json.get("record_id") != record["id"]
            or variant.metadata_json.get("rights_evidence") != policy["rights_evidence"]
            or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
            or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
            or session.scalar(select(Audit).where(Audit.entity == variant.id, Audit.action == "target_approve"))
            or session.scalar(select(Dispatch).where(Dispatch.state.in_(ACTIVE)))
            or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))):
        raise AgentError("ARTICLE_APPROVAL_RECONCILE_NOT_SAFE", 409)
    # The public eligibility gate still rejects needs_attention. Only this
    # explicit, tightly checked recovery validates an ephemeral pending view.
    _eligible_record(cycle, session, variant, policy, {**record, "state": "automatic_pending_approval"})
    snapshot = copy.deepcopy(core.snapshot(variant, target))
    if fingerprint(snapshot) != reviewed_hash or record.get("prepared_hash") != reviewed_hash:
        raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
    source = source_data(session, variant)
    material = session.get(Material, variant.sources[0]["id"])
    if (material.source_id != policy["source_id"] or material.published_at != record.get("source_published_at")
            or source["url"] != record.get("url") or original_hash(source["body"]) != record.get("original_hash")):
        raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
    configured_source, configured_target = cycle._source_target(policy)
    cycle._article({"url": source["url"], "title": source["title"], "original": source["body"],
                    "published_at": material.published_at}, policy, configured_source, configured_target)
    link = session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
    remote = session.get(RemoteDraft, variant.id)
    if (not link or not remote or remote.state != "draft" or remote.external_id != record.get("postiz_id")
            or link.post_id != record.get("postiz_id") or not link.active or link.error
            or link.remote_state != "QUEUE" or link.integration_id != target.options.get("postiz_integration_id")
            or snapshot["metadata"].get("postiz_post_id") != link.post_id
            or snapshot["metadata"].get("postiz_hash") != link.remote_hash
            or snapshot["metadata"].get("postiz_state") != "QUEUE"
            or any(type(value) not in {int, float} or not math.isfinite(value) or abs(value - due_at) > 0.001
                   for value in (record.get("due_at"), link.due_at, snapshot["metadata"].get("postiz_due_at")))):
        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
    evidence = automatic_evidence(session, variant)
    trace = inside(core.settings.root, record.get("quality_trace", ""))
    if (not trace.is_relative_to(cycle.directory) or not trace.name.startswith("quality-")
            or hashlib.sha256(trace.read_bytes()).hexdigest() != variant.metadata_json["machine_review_evidence"]["trace_hash"]):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    core._quality(session, variant, target)
    cycle._cadence(policy, due_at)
    core._check_schedule_limits(session, variant, target, due_at, automatic_policy=policy)
    return {"snapshot": snapshot, "postiz_id": link.post_id, "integration_id": link.integration_id,
            "remote_hash": link.remote_hash, "evidence_hash": evidence["evidence_hash"],
            "source_hash": fingerprint(source), "due_at": due_at}


def _reconcile_remote(cycle, captured):
    core = cycle.core
    remote = core.postiz.get_post(captured["postiz_id"])
    body, media, due, hashed = core.bridge.normalize(remote)
    snapshot = captured["snapshot"]
    integrations = [value for value in core.postiz.integrations() if value.get("id") == captured["integration_id"]]
    if (len(integrations) != 1 or integrations[0].get("identifier") != "max-browser"
            or integrations[0].get("accountId") != snapshot["destination"]
            or integrations[0].get("disabled") or integrations[0].get("refreshNeeded")):
        raise AgentError("BRIDGE_INTEGRATION_UNAVAILABLE", 409)
    if (remote.get("id") != captured["postiz_id"] or remote.get("state") != "QUEUE"
            or remote.get("integration", {}).get("id") != captured["integration_id"]
            or body != snapshot["body"] or media or hashed != captured["remote_hash"]
            or not math.isfinite(due) or abs(due - captured["due_at"]) > 0.001):
        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)


def reconcile_approval(cycle, record, ledger, reviewed_hash, due_at, reason):
    """Approve one proven scheduled post once; never repeat its remote mutation."""
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
        raise AgentError("ARTICLE_APPROVAL_RECONCILE_REASON_REQUIRED", 409)
    core, policy = cycle.core, cycle._policy_for_record(record)
    if (policy.get("approval_mode") != "automatic" or record.get("state") != "needs_attention"
            or record.get("error") != "SCHEDULE_GAP_TOO_SHORT" or not record.get("variant_id")
            or not record.get("postiz_id") or record.get("dispatch_id") or "automatic_slot" not in record):
        raise AgentError("ARTICLE_APPROVAL_RECONCILE_NOT_SAFE", 409)
    if (type(due_at) not in {int, float} or not math.isfinite(due_at)
            or not now() + 30 <= due_at <= now() + 3700):
        raise AgentError("ARTICLE_AUTOMATIC_DATE_INVALID", 409)
    if due_at != record.get("due_at"):
        raise AgentError("ARTICLE_APPROVAL_RECONCILE_NOT_SAFE", 409)
    claim_id = str(uuid5(NAMESPACE_URL, "max-postiz:approval-reconcile:" + record["id"]))
    history = record.get("approval_reconciliations", [])
    if not isinstance(history, list):
        raise AgentError("ARTICLE_LEDGER_INVALID", 409)
    with core.db.session() as session:
        if history or session.get(Audit, claim_id) or session.scalar(select(Audit).where(
                Audit.entity == record["id"], Audit.action == "hourly_article_approval_reconcile_started")):
            raise AgentError("ARTICLE_APPROVAL_RECONCILE_ALREADY_ATTEMPTED", 409)
        captured = _reconcile_capture(cycle, session, record, policy, reviewed_hash, due_at)
    _reconcile_remote(cycle, captured)
    claim = {"id": claim_id, "at": now(), "state": "started", "reason": reason.strip(),
             "original_error": record["error"], "revision": captured["snapshot"]["revision"],
             "content_hash": reviewed_hash, "due_at": due_at, "postiz_id": captured["postiz_id"],
             "target_id": captured["snapshot"]["target_id"], "destination": captured["snapshot"]["destination"],
             "policy_hash": fingerprint(policy), "evidence_hash": captured["evidence_hash"],
             "source_hash": captured["source_hash"], "remote_hash": captured["remote_hash"]}
    try:
        with core.db.session.begin() as session:
            fresh = _reconcile_capture(cycle, session, record, policy, reviewed_hash, due_at)
            if fingerprint(fresh) != fingerprint(captured):
                raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
            session.add(Audit(id=claim_id, actor=POLICY_ACTOR, action="hourly_article_approval_reconcile_started",
                              entity=record["id"], details=copy.deepcopy(claim)))
    except IntegrityError as exc:
        raise AgentError("ARTICLE_APPROVAL_RECONCILE_ALREADY_ATTEMPTED", 409) from exc
    try:
        record["approval_reconciliations"] = [*history, claim]
        record["state"] = "automatic_pending_approval"
        cycle._save(ledger)
        _reconcile_remote(cycle, captured)
        if fingerprint(cycle._policy_for_record(record)) != fingerprint(policy) or due_at < now() + 30:
            raise AgentError("ARTICLE_APPROVAL_RECONCILE_NOT_SAFE", 409)
        dispatch_id = core.approve_hourly_policy(record["variant_id"], claim["revision"], reviewed_hash, due_at)
        claim.update(state="completed", finished_at=now(), dispatch_id=dispatch_id)
        record.update(state="pending", dispatch_id=dispatch_id)
        record.pop("error", None)
        cycle._save(ledger)
        with core.db.session.begin() as session:
            core.db.audit(session, POLICY_ACTOR, "hourly_article_approval_reconcile_completed", record["id"], **claim)
        return cycle._view(record)
    except Exception as exc:
        claim.update(state="failed", finished_at=now(), error=exc.code if isinstance(exc, AgentError)
                     else "ARTICLE_APPROVAL_RECONCILE_INTERRUPTED")
        record.update(state="needs_attention", error=claim["error"])
        core.pause_target(record["target_id"], True, actor=POLICY_ACTOR)
        cycle._save(ledger)
        with core.db.session.begin() as session:
            core.db.audit(session, POLICY_ACTOR, "hourly_article_approval_reconcile_failed", record["id"], **claim)
        raise
