"""Explicit automatic approvals; never impersonates a human editor."""
import copy
import hashlib
import re
import time
from types import SimpleNamespace

from sqlalchemy import select

from .adapters import extract_object
from .config import AgentError, fingerprint
from .db import Audit, Material, now
from .hub_models import BridgePost, Dispatch, TargetApproval
from .human_review import candidate_body, machine_current, source_data, text_hash

POLICY_ACTOR = "hourly_article_policy"


def _validate(evidence, title, body, source):
    from .article_quality import (MEANING_FIRST_REVIEW_RUBRIC, TOPICS, normalize_rubric_review,
                                  rubric_spelling, rubric_translation_gate)
    required = {"review_raw", "review", "spelling_matches", "spelling_adjudicated", "spelling_unresolved",
                "hard_gates_passed", "text_hash", "source_hash", "review_rubric", "review_prompt_hash", "trace_hash"}
    if (not isinstance(evidence, dict) or set(evidence) != required
            or evidence["review_rubric"] != MEANING_FIRST_REVIEW_RUBRIC
            or evidence["hard_gates_passed"] is not True
            or evidence["text_hash"] != text_hash(title, body)
            or evidence["source_hash"] != fingerprint({**source, "title": source["title"].strip(),
                                                       "body": source["body"].strip()})
            or any(not isinstance(evidence[key], str) or not re.fullmatch(r"[0-9a-f]{64}", evidence[key])
                   for key in ("review_prompt_hash", "trace_hash"))):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    rubric = evidence["review_rubric"]
    review = normalize_rubric_review(evidence["review"], rubric)
    if (normalize_rubric_review(extract_object(evidence["review_raw"]), rubric) != review
            or review["approved"] is not True or review["issues"] or review["topic"] not in TOPICS):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    candidate = title + "\n\n" + candidate_body(SimpleNamespace(title=title, body=body), source)
    original = source["title"] + "\n\n" + source["body"]
    rubric_translation_gate(original, candidate, rubric)
    if len(body) > 3500 or source["url"] not in body:
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    matches = evidence["spelling_matches"]
    if not isinstance(matches, list) or any(not isinstance(value, dict) for value in matches):
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    adjudicated, unresolved = rubric_spelling(original, candidate, matches, rubric)
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
    return {"machine_review_status": "passed", "machine_review_issues": [],
            "machine_review_warnings": review["warnings"], "machine_review_evidence": evidence,
            "machine_review_evidence_hash": fingerprint(evidence), "review_rubric": evidence["review_rubric"],
            "review_prompt_hash": evidence["review_prompt_hash"]}


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
                or variant.metadata_json.get("review_prompt_hash") != evidence["review_prompt_hash"]):
            return None
        return {"evidence_hash": hashed, "review": copy.deepcopy(review), "warnings": review["warnings"]}
    except (AgentError, ValueError, TypeError, KeyError):
        return None


def eligible(core, session, variant):
    from .article_cycle import HourlyArticleCycle
    cycle = HourlyArticleCycle(core)
    policy = cycle._policy()
    if (policy.get("approval_mode") != "automatic"
            or variant.metadata_json.get("approval_mode") != "automatic"
            or variant.metadata_json.get("origin") != "hourly_article_test"
            or variant.target_id != policy["target_id"]
            or variant.metadata_json.get("policy_hash") != fingerprint(policy)):
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    cycle._source_target(policy)
    if variant.media:
        raise AgentError("ARTICLE_MEDIA_NOT_ALLOWED", 409)
    record = next((value for value in cycle._load()["records"] if value.get("variant_id") == variant.id), None)
    if not record or record["state"] not in {"automatic_ready", "automatic_pending_approval", "pending", "sending", "awaiting_postiz"}:
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    material = session.get(Material, variant.sources[0].get("id")) if len(variant.sources) == 1 else None
    if (not material or material.published_at is None
            or material.published_at < now() - policy["max_source_age_hours"] * 3600):
        raise AgentError("STALE_SOURCE", 409)
    if automatic_evidence(session, variant) is None:
        raise AgentError("ARTICLE_AUTOMATIC_RECHECK_REQUIRED", 409)
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
    core, policy = cycle.core, cycle._policy()
    if policy.get("approval_mode") != "automatic":
        raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
    if record["state"] not in {"prepared", "automatic_ready"}:
        raise AgentError("ARTICLE_AUTOMATIC_NOT_READY", 409)
    slot = int(now() // 3600)
    # A completed, unpublished draft may cross an hour boundary while models
    # or Postiz finish. Reserve this hour for that one draft, instead of trying
    # to catch up missed source slots or replay a late dispatch.
    if record["slot"] > slot:
        raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
    if any(item is not record and (item["slot"] == slot or item.get("automatic_slot") == slot)
           for item in ledger["records"]):
        raise AgentError("ARTICLE_SLOT_ALREADY_CLAIMED", 409)
    cycle._clear_outstanding(record["target_id"])
    earliest = cycle._cadence(policy, now(), check_gap=False)
    due = float(max(now() + 120, earliest) if due_at is None else due_at)
    if not now() + 30 <= due <= now() + 3700:
        raise AgentError("ARTICLE_AUTOMATIC_DATE_INVALID", 409)
    cycle._cadence(policy, due)
    with core.db.session() as session:
        variant = core._variant(session, record["variant_id"])
        target = core._target(session, variant.target_id)
        eligible(core, session, variant)
        before = copy.deepcopy(core.snapshot(variant, target))
        if reviewed_hash != fingerprint(before) or record["policy_hash"] != fingerprint(policy):
            raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
        # No prior approved or uncertain outcome may be repurposed.
        if (session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
                or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
                or automatic_evidence(session, variant) is None):
            raise AgentError("ARTICLE_AUTOMATIC_NOT_READY", 409)
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
