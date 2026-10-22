"""Exact-text machine evidence for the human-reviewed hourly route."""
import copy
import hashlib
import re

from sqlalchemy import select

from .article_attribution import article_footer, requires_licensed_footer
from .config import AgentError, fingerprint
from .browser_runtime import profile_lock
from .db import Audit, Material, now
from .hub_models import Dispatch, Target, Variant
from .reddit import text_hash as translation_hash


HUMAN_ACTORS = {"human_editor:owner", "human_editor:editor"}


class CandidateOnlyTranslator:
    model = "human_editor_candidate"

    def __init__(self):
        self.client = self

    def generate_text(self, _):
        raise AgentError("ARTICLE_TRANSLATION_NOT_ALLOWED", 409)

    def close(self):
        pass


def hourly(variant):
    return variant.metadata_json.get("origin") == "hourly_article_test"


def text_hash(title, body):
    return fingerprint({"title": title, "body": body})


def source_data(session, variant):
    if len(variant.sources) != 1:
        raise AgentError("ARTICLE_SOURCE_REQUIRED", 409)
    material = session.get(Material, variant.sources[0].get("id"))
    if not material or material.url != variant.sources[0].get("url"):
        raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
    return {"title": material.title, "body": material.text, "url": material.url}


def machine_current(session, variant):
    metadata = variant.metadata_json
    return (metadata.get("machine_text_hash") == text_hash(variant.title, variant.body)
            and metadata.get("machine_source_hash") == fingerprint(source_data(session, variant)))


def _validate_evidence(evidence, title, body, source, status):
    from .adapters import extract_object
    from .article_quality import TOPICS, _normalize_review, adjudicate_scientific_spelling
    from .editorial import translation_gate
    required = {"review_raw", "review", "spelling_matches", "spelling_adjudicated",
                "spelling_unresolved", "hard_gates_passed", "text_hash", "source_hash", "trace_hash"}
    if (not isinstance(evidence, dict) or set(evidence) not in (required, required | {"segment_hash"})
            or evidence.get("hard_gates_passed") is not True
            or evidence.get("text_hash") != text_hash(title, body)
            or evidence.get("source_hash") != fingerprint(source)
            or not isinstance(evidence.get("trace_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["trace_hash"])
            or not isinstance(evidence.get("review_raw"), str)):
        raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
    if "segment_hash" in evidence and not re.fullmatch(r"[0-9a-f]{64}", evidence["segment_hash"]):
        raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
    reviewed = _normalize_review(evidence["review"])
    if (_normalize_review(extract_object(evidence["review_raw"])) != reviewed
            or reviewed["topic"] not in TOPICS
            or status not in {"passed", "rejected"}
            or (status == "passed" and (not reviewed["approved"] or reviewed["issues"]))
            or (status == "rejected" and (reviewed["approved"] or not reviewed["issues"]))):
        raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
    matches = evidence["spelling_matches"]
    if not isinstance(matches, list) or any(not isinstance(x, dict) for x in matches):
        raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
    adjudicated, unresolved = adjudicate_scientific_spelling(
        source["title"] + "\n\n" + source["body"], title + "\n\n" + candidate_body(
            type("Candidate", (), {"title": title, "body": body})(), source), matches)
    if (unresolved or evidence["spelling_unresolved"] != unresolved
            or evidence["spelling_adjudicated"] != adjudicated):
        raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
    candidate = candidate_body(type("Candidate", (), {"title": title, "body": body})(), source)
    translation_gate(source["title"] + "\n\n" + source["body"], title + "\n\n" + candidate)
    if len(body) > 3500:
        raise AgentError("ARTICLE_POST_TOO_LONG", 409)
    return reviewed


def machine_metadata(prepared, source, trace_path):
    """Keep a negative verdict negative, bound to the exact staged text and source."""
    if "machine_review_evidence" not in prepared:
        if prepared.get("machine_review_status") == "rejected":
            raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
        return {}  # Historical passed records and explicit offline test doubles.
    evidence = copy.deepcopy(prepared["machine_review_evidence"])
    trace_hash = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    if evidence.get("trace_hash", trace_hash) != trace_hash:
        raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
    evidence["trace_hash"] = trace_hash
    status = prepared["machine_review_status"]
    reviewed = _validate_evidence(evidence, prepared["title"], prepared["body"], source, status)
    return {"machine_review_status": status, "machine_review_issues": reviewed["issues"],
            "machine_review_evidence": evidence, "machine_review_evidence_hash": fingerprint(evidence)}


def rejected_evidence(session, variant):
    if variant.metadata_json.get("machine_review_status") != "rejected":
        return None
    try:
        if not machine_current(session, variant):
            return None
        evidence = variant.metadata_json.get("machine_review_evidence")
        reviewed = _validate_evidence(evidence, variant.title, variant.body,
                                      source_data(session, variant), "rejected")
        hashed = fingerprint(evidence)
        if hashed != variant.metadata_json.get("machine_review_evidence_hash"):
            return None
        return {"evidence_hash": hashed, "issues": copy.deepcopy(reviewed["issues"]),
                "review": copy.deepcopy(reviewed)}
    except (AgentError, ValueError, KeyError, TypeError):
        return None


def eligible(core, session, variant):
    from .article_cycle import HourlyArticleCycle
    cycle = HourlyArticleCycle(core)
    policy = cycle._policy()
    if (policy.get("approval_mode") != "human"
            or variant.metadata_json.get("approval_mode") != "human"
            or variant.target_id != policy["target_id"]
            or variant.metadata_json.get("policy_hash") != fingerprint(policy)):
        raise AgentError("ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE", 409)
    if variant.media:
        raise AgentError("ARTICLE_MEDIA_NOT_ALLOWED", 409)
    record = next((item for item in cycle._load()["records"]
                   if item.get("variant_id") == variant.id), None)
    if not record or record["state"] not in {"awaiting_human_review", "pending", "sending", "awaiting_postiz"}:
        raise AgentError("ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE", 409)
    material = session.get(Material, variant.sources[0].get("id")) if variant.sources else None
    if (not material or material.published_at is None
            or material.published_at < now() - policy["max_source_age_hours"] * 3600):
        raise AgentError("STALE_SOURCE", 409)
    return policy


def details(core, ident):
    from .hub import ACTIVE
    with core.db.session() as session:
        variant = core._variant(session, ident)
        if not hourly(variant):
            return {"hourly_article": False}
        source = source_data(session, variant)
        current = machine_current(session, variant)
        delivery = session.scalar(select(Dispatch).where(Dispatch.variant_id == ident)
                                  .order_by(Dispatch.state.in_(ACTIVE).desc(),
                                            (Dispatch.state == "published").desc(), Dispatch.due_at.desc()))
        automatic = variant.metadata_json.get("approval_mode") == "automatic"
        automatic_proof = None
        if automatic:
            from .automatic_policy import automatic_evidence
            automatic_proof = automatic_evidence(session, variant)
        status = (delivery.state if delivery else
                  "automatic_ready" if automatic_proof else "automatic_blocked" if automatic else
                  "awaiting_human_review" if current else "machine_recheck_required")
        if automatic and delivery and delivery.state == "published" and not delivery.verified:
            status = "published_unverified"
        return {"hourly_article": True, "human_review_required": not automatic,
                "automatic_policy": automatic,
                "automatic_machine_review_valid": automatic_proof is not None,
                "source_title": source["title"], "source_body": source["body"],
                "source_url": source["url"], "machine_review_current": current,
                "machine_review_status": variant.metadata_json.get(
                    "machine_review_status", "passed" if current else "unconfirmed"),
                "machine_review_issues": variant.metadata_json.get("machine_review_issues", []),
                "machine_review_warnings": copy.deepcopy(variant.metadata_json.get("machine_review_warnings", [])),
                "review_rubric": variant.metadata_json.get("review_rubric"),
                "review_prompt_hash": variant.metadata_json.get("review_prompt_hash"),
                "machine_exception_eligible": not automatic and rejected_evidence(session, variant) is not None,
                "machine_review_evidence": copy.deepcopy(variant.metadata_json.get("machine_review_evidence")),
                "machine_review_evidence_hash": variant.metadata_json.get("machine_review_evidence_hash"),
                "target_paused": session.get(Target, variant.target_id).paused,
                "dispatch_state": delivery.state if delivery else None,
                "delivery_verified": delivery.verified if delivery else False,
                "status": status, "revision": variant.revision,
                "content_hash": fingerprint(core.snapshot(variant, session.get(Target, variant.target_id)))}


def candidate_body(variant, source):
    body = variant.body
    # Postiz's plain-text extraction may turn a paragraph separator into one LF.
    # Match only the exact staged title and our complete terminal attribution.
    for separator in ("\n\n", "\n"):
        prefix = variant.title + separator
        if body.startswith(prefix) and body[len(prefix):] and not body[len(prefix):].startswith("\n"):
            body = body[len(prefix):]
            break
    footer = article_footer(source["url"])
    for separator in ("\n\n", "\n"):
        suffix = separator + footer
        if body.endswith(suffix) and not body[:-len(suffix)].endswith("\n"):
            return body[:-len(suffix)]
    if requires_licensed_footer(source["url"]):
        # Потерянная лицензия или attribution не снимаются старым NASA-форматом.
        raise AgentError("ARTICLE_ATTRIBUTION_REQUIRED", 409)
    # Keep the pre-existing two-LF legacy attribution contract. A single-LF
    # boundary is accepted only with the complete service disclosure above.
    legacy_footer = "\n\nИсточник: " + source["url"]
    if body.endswith(legacy_footer) and not body[:-len(legacy_footer)].endswith("\n"):
        return body[:-len(legacy_footer)]
    return body


def close_quality(quality, close_models=True):
    """Close every owned client even when another cleanup fails."""
    try:
        quality.close()
    finally:
        if close_models:
            try:
                quality.translator.client.close()
            finally:
                quality.reviewer.client.close()


def recheck(core, ident, revision, content_hash):
    directory = core.settings.root / "data/hourly-articles"
    directory.mkdir(parents=True, exist_ok=True)
    # The variant identifier is validated by its database lookup before use in a path.
    with core.db.session() as session:
        variant = core._variant(session, ident)
        lock_name = fingerprint({"variant_id": variant.id})
    with profile_lock(directory / ("human-recheck-" + lock_name + ".lock")):
        return _recheck(core, ident, revision, content_hash)


def _recheck(core, ident, revision, content_hash):
    from .article_cycle import HourlyArticleCycle
    core.bridge.sync(force=True)
    with core.db.session.begin() as session:
        variant = core._variant(session, ident, True)
        if not hourly(variant):
            raise AgentError("ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE", 409)
        eligible(core, session, variant)
        snapshot = core.snapshot(variant, session.get(Target, variant.target_id))
        if revision != variant.revision or content_hash != fingerprint(snapshot):
            raise AgentError("REVISION_CONFLICT", 409)
        source = source_data(session, variant)
        title, body = variant.title, candidate_body(variant, source)
        record_id = variant.metadata_json.get("record_id")
        candidate_hash = fingerprint({"title": title.strip(), "body": body.strip()})
        failures = session.scalars(select(Audit).where(Audit.entity == ident,
                                  Audit.action.in_({"hourly_article_recheck_failed", "hourly_article_rechecked",
                                                    "hourly_article_prepared"})))
        if rejected_evidence(session, variant) is not None or any((row.details.get("error") == "ARTICLE_QUALITY_REJECTED"
                or row.details.get("machine_review_status") == "rejected")
               and row.details.get("candidate_hash") == candidate_hash for row in failures):
            raise AgentError("ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED", 409)
        if session.scalar(select(Dispatch).where(Dispatch.variant_id == ident,
                          Dispatch.state.not_in({"cancelled", "failed"}))):
            raise AgentError("DELIVERY_ALREADY_EXISTS", 409)
        core._invalidate(session, variant)
        variant.metadata_json = {**variant.metadata_json, "machine_text_hash": None,
                                 "machine_review_status": "checking"}
        variant.revision += 1
        target = session.get(Target, variant.target_id)
        core._history(session, variant, target, "machine_recheck_started")
        revision, content_hash = variant.revision, fingerprint(core.snapshot(variant, target))
    cycle = HourlyArticleCycle(core)
    quality = None
    try:
        quality = cycle._quality(record_id, review_only=True)
        checked = quality.check_candidate(title, body, source["title"], source["body"], source["url"])
    except Exception as exc:
        error = exc.code if isinstance(exc, AgentError) else "ARTICLE_RECHECK_INTERRUPTED"
        with core.db.session.begin() as session:
            variant = core._variant(session, ident, True)
            if variant.revision == revision:
                variant.metadata_json = {**variant.metadata_json, "machine_review_status": "failed"}
                variant.revision += 1
                core._history(session, variant, session.get(Target, variant.target_id), "machine_recheck_failed")
            core.db.audit(session, "machine_recheck", "hourly_article_recheck_failed", ident,
                          candidate_hash=candidate_hash, error=error)
        raise
    finally:
        if quality:
            close_quality(quality)
    core.bridge.sync(force=True)
    with core.db.session.begin() as session:
        variant = core._variant(session, ident, True)
        target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
        if revision != variant.revision or content_hash != fingerprint(core.snapshot(variant, target)):
            raise AgentError("REVISION_CONFLICT", 409)
        eligible(core, session, variant)
        if source_data(session, variant) != source:
            raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
        translated_hash = translation_hash(checked["translation"])
        if (any(item.get("variant_id") != ident and item.get("translation_hash") == translated_hash
                for item in cycle._load()["records"])
                or session.scalar(select(Variant).where(Variant.target_id == target.id, Variant.id != ident,
                    Variant.metadata_json["translation_hash"].as_string() == translated_hash))):
            raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
        core._invalidate(session, variant)
        # Review the existing exact editor text. The checker's normalized export
        # is not a Postiz edit and must never silently replace that text locally.
        variant.revision += 1
        variant.state, variant.updated_at = "review_pending", now()
        metadata = {**variant.metadata_json, "quality": "operator_ministral_languagetool",
                    "translation_hash": translated_hash,
                    "machine_text_hash": text_hash(variant.title, variant.body),
                    "machine_source_hash": fingerprint(source), "machine_reviewed_at": now(),
                    "machine_review_status": "passed", "machine_review_issues": [],
                    **machine_metadata(checked, source, cycle.directory / ("quality-" + record_id + ".local.jsonl"))}
        metadata.pop("translation_model", None)
        metadata.pop("review_model", None)
        variant.metadata_json = metadata
        core._history(session, variant, target, "machine_recheck")
        core.db.audit(session, "machine_recheck", "hourly_article_rechecked", ident,
                      revision=variant.revision, machine_text_hash=metadata["machine_text_hash"],
                      candidate_hash=candidate_hash, machine_review_status=metadata["machine_review_status"],
                      machine_review_evidence_hash=metadata.get("machine_review_evidence_hash"))
    return details(core, ident)
