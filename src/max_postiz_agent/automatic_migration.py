"""One explicit, audited reclassification of an existing human article; never send it."""
import copy
import hashlib
import os
import re
from types import SimpleNamespace

from sqlalchemy import select

from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside
from .db import Audit, Material, now, uid
from .hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant
from .human_review import candidate_body, close_quality, source_data, text_hash
from .reddit import text_hash as translation_hash

RUBRIC = "meaning-first-v1"
ACTOR = "hourly_article_migration"


def _capture(cycle, session, record, policy, *, lock=False):
    core = cycle.core
    if (record.get("state") != "awaiting_human_review" or not record.get("variant_id")
            or record.get("target_id") != policy["target_id"]
            or record.get("source_id") != policy["source_id"]):
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_NOT_SAFE", 409)
    variant = core._variant(session, record["variant_id"], lock)
    target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update()) if lock else session.get(Target, variant.target_id)
    metadata = variant.metadata_json
    if (metadata.get("origin") != "hourly_article_test" or metadata.get("approval_mode") != "human"
            or metadata.get("record_id") != record["id"] or variant.target_id != policy["target_id"]
            or variant.state != "review_pending" or variant.media or not target
            or variant.binding_hash != target.binding_hash or variant.config_hash != core.settings.hash
            or metadata.get("rights_evidence") != policy["rights_evidence"]):
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_NOT_SAFE", 409)
    if (session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
            or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
            or session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                                                       BridgePost.mutation_owner.is_not(None)))):
        raise AgentError("ARTICLE_OUTSTANDING_DELIVERY", 409)
    source = source_data(session, variant)
    material = session.get(Material, variant.sources[0]["id"])
    if (material.source_id != policy["source_id"] or record.get("url") != source["url"]
            or record.get("original_hash") != translation_hash(source["body"])
            or record.get("source_published_at") != material.published_at):
        raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
    article = {"url": source["url"], "title": source["title"], "original": source["body"],
               "published_at": material.published_at}
    configured_source, configured_target = cycle._source_target(policy)
    cycle._article(article, policy, configured_source, configured_target)
    link = session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
    remote = session.get(RemoteDraft, variant.id)
    post_id = record.get("postiz_id")
    if (not link or not remote or not post_id or link.post_id != post_id
            or remote.external_id != post_id or remote.state != "draft"
            or metadata.get("postiz_post_id") != post_id
            or not link.active or link.error or link.mutation_owner
            or link.remote_state not in {"DRAFT", "QUEUE"}
            or link.integration_id != target.options.get("postiz_integration_id")
            or metadata.get("postiz_hash") != link.remote_hash
            or metadata.get("postiz_state") != link.remote_state):
        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
    return {"snapshot": copy.deepcopy(core.snapshot(variant, target)), "source": source,
            "post_id": post_id, "remote_hash": link.remote_hash,
            "integration_id": link.integration_id}


def _remote(cycle, captured):
    """GET and normalize only; do not sync, change dates, or acquire a mutation lease."""
    remote = cycle.core.postiz.get_post(captured["post_id"])
    body, images, _, hashed = cycle.core.bridge.normalize(remote)
    if (remote.get("id") != captured["post_id"] or remote.get("state") not in {"DRAFT", "QUEUE"}
            or remote.get("integration", {}).get("id") != captured["integration_id"]
            or images or body != captured["snapshot"]["body"] or hashed != captured["remote_hash"]):
        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)


def migrate(cycle, record_id, reason):
    """Re-evaluate one exact EN/RU pair once under the new rubric, with no approval."""
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_REASON_REQUIRED", 409)
    if not isinstance(record_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", record_id):
        raise AgentError("ARTICLE_RECORD_ID_INVALID", 409)
    reason = reason.strip()
    with profile_lock(cycle.directory / "cycle.lock"):
        return _migrate(cycle, record_id, reason)


def _migrate(cycle, record_id, reason):
    from .automatic_policy import machine_metadata

    core, policy = cycle.core, cycle._policy()
    if policy.get("approval_mode") != "automatic":
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    cycle._clear_outstanding(policy["target_id"])
    ledger = cycle._load()
    record = cycle._record(ledger, record_id)
    from .article_sources import require_legacy_source
    require_legacy_source(record)
    original_record = copy.deepcopy(record)
    with core.db.session() as session:
        captured = _capture(cycle, session, record, policy)
    _remote(cycle, captured)
    snapshot, source = captured["snapshot"], captured["source"]
    prompt = inside(core.settings.root, "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt")
    try:
        prompt_hash = hashlib.sha256(prompt.read_text(encoding="utf-8").encode()).hexdigest()
    except OSError as exc:
        raise AgentError("ARTICLE_AUTOMATIC_RUBRIC_REQUIRED", 409) from exc
    key = fingerprint({"record_id": record_id, "source_hash": fingerprint(source),
        "text_hash": text_hash(snapshot["title"], snapshot["body"]),
        "review_rubric": RUBRIC})
    attempts = record.get("automatic_migrations", [])
    if not isinstance(attempts, list):
        raise AgentError("ARTICLE_LEDGER_INVALID", 409)
    with core.db.session() as session:
        previous = list(session.scalars(select(Audit).where(Audit.entity == record_id,
            Audit.action == "hourly_article_automatic_migration_started")))
    if any(not isinstance(item, dict) or item.get("key") == key for item in attempts) or any(
            row.details.get("key") == key for row in previous):
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_ALREADY_ATTEMPTED", 409)
    old_trace = record.get("quality_trace")
    if old_trace != f"data/hourly-articles/quality-{record_id}.local.jsonl":
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_TRACE_REQUIRED", 409)
    old_path = inside(core.settings.root, old_trace)
    try:
        old_bytes = old_path.read_bytes()
    except OSError as exc:
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_TRACE_REQUIRED", 409) from exc
    if not old_bytes:
        raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_TRACE_REQUIRED", 409)
    ident = uid()
    trace = f"data/hourly-articles/quality-{ident}.local.jsonl"
    backup = cycle.directory / (f"migration-{ident}.before.local.jsonl")
    with backup.open("xb") as stream:
        backup.chmod(0o600)
        stream.write(old_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    attempt = {"id": ident, "key": key, "started_at": now(), "state": "checking", "reason": reason,
        "review_rubric": RUBRIC, "review_prompt_hash": prompt_hash,
        "previous_policy_hash": record.get("policy_hash"), "policy_hash": fingerprint(policy),
        "previous_content_hash": fingerprint(snapshot), "previous_trace": old_trace,
        "previous_trace_hash": hashlib.sha256(old_bytes).hexdigest(),
        "previous_trace_backup": backup.relative_to(core.settings.root).as_posix(),
        "quality_trace": trace, "previous_metadata": copy.deepcopy(snapshot["metadata"])}
    record["automatic_migrations"] = [*attempts, attempt]
    # Claim before model construction/inference. Interruptions consume this rubric/text pair.
    cycle._save(ledger)
    database_updated = False
    try:
        with core.db.session.begin() as session:
            core.db.audit(session, ACTOR, "hourly_article_automatic_migration_started", record_id,
                key=key, reason=reason, variant_id=snapshot["variant_id"], revision=snapshot["revision"],
                content_hash=fingerprint(snapshot), review_rubric=RUBRIC, review_prompt_hash=prompt_hash,
                old_trace_hash=attempt["previous_trace_hash"], policy_hash=fingerprint(policy))
        quality = cycle._quality(ident, review_only=True)
        try:
            if (getattr(quality, "review_rubric", None) != RUBRIC
                    or getattr(quality, "review_prompt_hash", None) != prompt_hash):
                raise AgentError("ARTICLE_AUTOMATIC_RUBRIC_REQUIRED", 409)
            body = candidate_body(SimpleNamespace(title=snapshot["title"], body=snapshot["body"]), source)
            checked = quality.check_candidate(snapshot["title"], body,
                                               source["title"], source["body"], source["url"])
        finally:
            close_quality(quality, close_models=not cycle.quality_factory)
        if checked.get("machine_review_status") != "passed":
            raise AgentError("ARTICLE_AUTOMATIC_QUALITY_REJECTED", 409)
        if (checked.get("title") != snapshot["title"] or checked.get("body") != snapshot["body"]
                or checked.get("url") != source["url"]
                or checked.get("original") != source["title"] + "\n\n" + source["body"]):
            raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_TEXT_CHANGED", 409)
        metadata = machine_metadata(checked, source, inside(core.settings.root, trace))
        if (old_path.read_bytes() != old_bytes or fingerprint(cycle._policy()) != fingerprint(policy)
                or hashlib.sha256(prompt.read_text(encoding="utf-8").encode()).hexdigest() != prompt_hash):
            raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_INPUT_CHANGED", 409)
        _remote(cycle, captured)
        cycle._clear_outstanding(policy["target_id"])
        with core.db.session.begin() as session:
            fresh = _capture(cycle, session, record, policy, lock=True)
            if fingerprint(fresh) != fingerprint(captured):
                raise AgentError("REVISION_CONFLICT", 409)
            variant = core._variant(session, snapshot["variant_id"], True)
            target = session.get(Target, variant.target_id)
            translated_hash = translation_hash(checked["translation"])
            if (any(item.get("variant_id") != variant.id and item.get("translation_hash") == translated_hash
                    for item in ledger["records"])
                    or session.scalar(select(Variant).where(Variant.target_id == target.id, Variant.id != variant.id,
                        Variant.metadata_json["translation_hash"].as_string() == translated_hash))):
                raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
            history = copy.deepcopy(variant.metadata_json.get("automatic_migration_history", []))
            if not isinstance(history, list):
                raise AgentError("ARTICLE_AUTOMATIC_MIGRATION_NOT_SAFE", 409)
            history.append({**copy.deepcopy(attempt), "state": "completed", "finished_at": now(),
                            "machine_review_evidence_hash": metadata["machine_review_evidence_hash"]})
            variant.metadata_json = {**variant.metadata_json, **metadata,
                "approval_mode": "automatic", "policy_hash": fingerprint(policy),
                "machine_text_hash": text_hash(variant.title, variant.body),
                "machine_source_hash": fingerprint(source), "machine_reviewed_at": now(),
                "translation_hash": translated_hash,
                "quality": "existing_translation_meaning_first_review_languagetool",
                "automatic_migration_history": history}
            variant.revision += 1
            variant.updated_at = now()
            core._history(session, variant, target, ACTOR)
            core.db.audit(session, ACTOR, "hourly_article_automatic_migrated", variant.id,
                record_id=record_id, key=key, reason=reason, revision=variant.revision,
                machine_review_evidence_hash=metadata["machine_review_evidence_hash"],
                previous_evidence_hash=snapshot["metadata"].get("machine_review_evidence_hash"),
                policy_hash=fingerprint(policy), review_rubric=RUBRIC, no_approval=True, no_dispatch=True)
            record.update(approval_mode="automatic", policy_hash=fingerprint(policy),
                automatic_enrolled=True, state="automatic_ready", quality_trace=trace,
                translation_hash=translated_hash, prepared_hash=fingerprint(core.snapshot(variant, target)))
        database_updated = True
        attempt.update(state="completed", finished_at=now(),
                       machine_review_evidence_hash=metadata["machine_review_evidence_hash"])
        cycle._save(ledger)
        return cycle._view(record)
    except Exception as exc:
        code = exc.code if isinstance(exc, AgentError) else "ARTICLE_AUTOMATIC_MIGRATION_INTERRUPTED"
        attempt.update(state="failed", finished_at=now(), error=code)
        if not database_updated:
            migrations = record["automatic_migrations"]
            record.clear()
            record.update(original_record)
            record["automatic_migrations"] = migrations
        record["state"] = "needs_attention" if database_updated else "awaiting_human_review"
        cycle._save(ledger)
        with core.db.session.begin() as session:
            core.db.audit(session, ACTOR, "hourly_article_automatic_migration_failed", record_id,
                          key=key, reason=reason, error=code, quality_trace=trace)
        if isinstance(exc, AgentError):
            raise
        raise AgentError(code, 503) from None
