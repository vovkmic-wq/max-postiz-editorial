"""One audited correction of an existing automatic DRAFT; never approve or send."""
import copy
import hashlib
import json
import os
import re
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .automatic_policy import automatic_evidence, machine_metadata
from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside
from .db import Audit, Draft, DraftRevision, Material, now, uid
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant
from .human_review import candidate_body, close_quality, source_data, text_hash
from .reddit import text_hash as translation_hash

ACTOR = "hourly_article_staged_correction"
RUBRIC = "meaning-first-v1"
STARTED = "hourly_staged_correction_started"


def _quiet(core, session, variant_id):
    if (session.scalar(select(Target).where(Target.paused.is_(False)))
            or session.scalar(select(Dispatch).where(Dispatch.state.in_(ACTIVE)))
            or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))
            or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant_id))
            or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant_id))):
        raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_NOT_QUIET", 409)


def _capture(cycle, session, record, policy, state):
    core = cycle.core
    if (record.get("state") != state or record.get("slot") != int(now() // 3600)
            or record.get("approval_mode") not in {None, "automatic"}
            or record.get("policy_hash") != fingerprint(policy)
            or record.get("target_id") != policy["target_id"]
            or record.get("source_id") != policy["source_id"]
            or not record.get("variant_id") or record.get("automatic_slot") is not None
            or record.get("due_at") is not None or record.get("dispatch_id")):
        raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_NOT_SAFE", 409)
    variant = core._variant(session, record["variant_id"], True)
    target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
    metadata = variant.metadata_json
    expected_mode = "automatic" if state == "automatic_ready" else "automatic_correction_pending"
    if (not target or metadata.get("origin") != "hourly_article_test"
            or metadata.get("approval_mode") != expected_mode or metadata.get("record_id") != record["id"]
            or metadata.get("policy_hash") != fingerprint(policy)
            or metadata.get("rights_evidence") != policy["rights_evidence"]
            or variant.target_id != policy["target_id"] or variant.state != "review_pending"
            or variant.media or variant.config_hash != core.settings.hash
            or variant.binding_hash != target.binding_hash or automatic_evidence(session, variant) is None):
        raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_NOT_SAFE", 409)
    _quiet(core, session, variant.id)
    source = source_data(session, variant)
    material = session.get(Material, variant.sources[0]["id"])
    if (material.source_id != policy["source_id"] or record.get("url") != source["url"]
            or record.get("original_hash") != translation_hash(source["body"])
            or record.get("source_published_at") != material.published_at):
        raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
    configured_source, configured_target = cycle._source_target(policy)
    cycle._article({"url": source["url"], "title": source["title"], "original": source["body"],
                    "published_at": material.published_at}, policy, configured_source, configured_target)
    link = session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
    remote = session.get(RemoteDraft, variant.id)
    if (not link or not remote or not record.get("postiz_id")
            or link.post_id != record["postiz_id"] or remote.external_id != link.post_id
            or remote.state != "draft" or not link.active or link.error or link.mutation_owner
            or link.remote_state != "DRAFT" or metadata.get("postiz_state") != "DRAFT"
            or metadata.get("postiz_post_id") != link.post_id
            or metadata.get("postiz_hash") != link.remote_hash
            or link.integration_id != target.options.get("postiz_integration_id")):
        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
    return {"snapshot": copy.deepcopy(core.snapshot(variant, target)), "source": source,
            "post_id": link.post_id, "integration_id": link.integration_id, "remote_hash": link.remote_hash}


def _remote(cycle, captured, expected_body, before=None):
    remote = cycle.core.postiz.get_post(captured["post_id"])
    body, images, _, hashed = cycle.core.bridge.normalize(remote)
    if (remote.get("id") != captured["post_id"] or remote.get("state") != "DRAFT"
            or remote.get("integration", {}).get("id") != captured["integration_id"]
            or images or body != expected_body):
        raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
    if before is None:
        if hashed != captured["remote_hash"]:
            raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
    else:
        # Postiz may assign a new group on update. Every other envelope field is fixed.
        for key in ("publishDate", "settings", "integration", "intervalInDays", "childrenPost"):
            if remote.get(key) != before.get(key):
                raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
    return remote


def _save_private(path, raw):
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def correct_staged(cycle, ident, correction, reviewed_hash, reason):
    """Consume one durable claim before checking a changed, source-bound candidate."""
    cycle._validate_correction(correction)
    if (not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9-]{36}", ident)
            or not isinstance(reviewed_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", reviewed_hash)):
        raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
        raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_REASON_REQUIRED", 409)
    correction = {key: value.strip() for key, value in correction.items()}
    with profile_lock(cycle.directory / "cycle.lock"):
        return _correct(cycle, ident, correction, reviewed_hash, reason.strip())


def _correct(cycle, ident, correction, reviewed_hash, reason):
    core, policy = cycle.core, cycle._policy()
    if policy.get("approval_mode") != "automatic" or policy.get("review_rubric") != RUBRIC:
        raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 409)
    ledger = cycle._load()
    record = cycle._record(ledger, ident)
    claim_id = str(uuid5(NAMESPACE_URL, "max-postiz:staged-automatic-correction:" + ident))
    with core.db.session() as session:
        if record.get("automatic_corrections") or session.get(Audit, claim_id):
            raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED", 409)
        captured = _capture(cycle, session, record, policy, "automatic_ready")
    snapshot, source = captured["snapshot"], captured["source"]
    if reviewed_hash != fingerprint(snapshot) or reviewed_hash != record.get("prepared_hash"):
        raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
    old_candidate = {"title": snapshot["title"], "body": candidate_body(
        SimpleNamespace(title=snapshot["title"], body=snapshot["body"]), source)}
    if cycle._candidate_hash(correction) == cycle._candidate_hash(old_candidate):
        raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_UNCHANGED", 409)
    cycle._guard_candidate_replay(ledger, record, correction, "correct_staged")
    old_trace = record.get("quality_trace", "")
    if not isinstance(old_trace, str) or not re.fullmatch(
            r"data/hourly-articles/quality-[a-f0-9-]{36}\.local\.jsonl", old_trace):
        raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_TRACE_REQUIRED", 409)
    old_path = inside(core.settings.root, old_trace)
    old_bytes = old_path.read_bytes()
    old_hash = hashlib.sha256(old_bytes).hexdigest()
    if not old_bytes or snapshot["metadata"]["machine_review_evidence"].get("trace_hash") != old_hash:
        raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
    operation = uid()
    trace = f"data/hourly-articles/quality-{operation}.local.jsonl"
    backup = cycle.directory / f"correction-{operation}.before.local.json"
    trace_backup = cycle.directory / f"correction-{operation}.before.local.jsonl"
    _save_private(backup, json.dumps({"record": copy.deepcopy(record), "captured": captured},
                                   ensure_ascii=False, sort_keys=True).encode("utf-8"))
    _save_private(trace_backup, old_bytes)
    attempt = {"id": operation, "claim_id": claim_id, "started_at": now(), "state": "checking",
               "reason": reason, "candidate_hash": cycle._candidate_hash(correction),
               "previous_content_hash": reviewed_hash, "previous_trace": old_trace,
               "previous_trace_hash": old_hash, "previous_snapshot_backup": backup.relative_to(core.settings.root).as_posix(),
               "previous_trace_backup": trace_backup.relative_to(core.settings.root).as_posix(),
               "quality_trace": trace, "policy_hash": fingerprint(policy), "postiz_id": captured["post_id"]}
    with core.db.session.begin() as session:
        try:
            if fingerprint(_capture(cycle, session, record, policy, "automatic_ready")) != fingerprint(captured):
                raise AgentError("REVISION_CONFLICT", 409)
            session.add(Audit(id=claim_id, actor=ACTOR, action=STARTED, entity=ident,
                              details=copy.deepcopy(attempt)))
            variant = core._variant(session, snapshot["variant_id"], True)
            variant.metadata_json = {**variant.metadata_json, "approval_mode": "automatic_correction_pending",
                                     "automatic_correction_claim": claim_id, "correction_blocked": True}
            variant.revision += 1
            core._history(session, variant, session.get(Target, variant.target_id), ACTOR)
            session.flush()
            claimed = {**captured, "snapshot": copy.deepcopy(core.snapshot(variant, session.get(Target, variant.target_id)))}
        except IntegrityError as exc:
            raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_ALREADY_ATTEMPTED", 409) from exc
    record.update(state="automatic_correcting", correction_blocked=True, automatic_corrections=[attempt])
    cycle._save(ledger)
    try:
        remote_before = _remote(cycle, captured, snapshot["body"])
        prompt = core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
        prompt_hash = hashlib.sha256(prompt.read_text(encoding="utf-8").encode()).hexdigest()
        quality = cycle._quality(operation, review_only=True)
        try:
            if quality.review_rubric != RUBRIC or quality.review_prompt_hash != prompt_hash:
                raise AgentError("ARTICLE_AUTOMATIC_RUBRIC_REQUIRED", 409)
            checked = quality.check_candidate(correction["title"], correction["body"],
                                               source["title"], source["body"], source["url"])
        finally:
            close_quality(quality, close_models=not cycle.quality_factory)
        if (checked.get("machine_review_status") != "passed" or checked.get("title") != correction["title"]
                or checked.get("url") != source["url"]
                or checked.get("original") != source["title"].strip() + "\n\n" + source["body"].strip()):
            raise AgentError("ARTICLE_AUTOMATIC_QUALITY_REJECTED", 409)
        metadata = machine_metadata(checked, source, inside(core.settings.root, trace))
        expected_translation = correction["title"] + "\n\n" + correction["body"]
        if (checked.get("translation") != expected_translation or checked.get("body") != expected_translation
                + "\n\nИсточник: " + source["url"] + "\nПеревод и редактура с помощью нейросетей."):
            raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_INPUT_CHANGED", 409)
        if (fingerprint(cycle._policy()) != fingerprint(policy) or old_path.read_bytes() != old_bytes
                or hashlib.sha256(prompt.read_text(encoding="utf-8").encode()).hexdigest() != prompt_hash):
            raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_INPUT_CHANGED", 409)
        with core.db.session() as session:
            if fingerprint(_capture(cycle, session, record, policy, "automatic_correcting")) != fingerprint(claimed):
                raise AgentError("REVISION_CONFLICT", 409)
            translated_hash = translation_hash(checked["translation"])
            if (any(item is not record and item.get("translation_hash") == translated_hash for item in ledger["records"])
                    or any(v.metadata_json.get("translation_hash") == translated_hash for v in session.scalars(
                        select(Variant).where(Variant.target_id == policy["target_id"], Variant.id != snapshot["variant_id"])))):
                raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
        _remote(cycle, captured, snapshot["body"])
        attempt["state"] = "remote_mutating"
        attempt["mutation_started_at"] = now()
        cycle._save(ledger)
        updated = core.postiz.update_existing_draft(captured["post_id"], snapshot["body"], checked["body"],
                                                   expected_hash=captured["remote_hash"])
        if updated != captured["post_id"]:
            raise AgentError("POSTIZ_UNKNOWN", 503)
        deadline = time.monotonic() + 5
        while True:
            with core.db.session() as session:
                link = session.get(BridgePost, captured["post_id"])
                if not link:
                    raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
                if not link.mutation_owner:
                    break
            if time.monotonic() >= deadline:
                raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
            time.sleep(0.05)
        remote_after = _remote(cycle, captured, checked["body"], remote_before)
        core.bridge.sync(force=True, target_id=policy["target_id"])
        with core.db.session.begin() as session:
            variant = core._variant(session, snapshot["variant_id"], True)
            target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
            _quiet(core, session, variant.id)
            fresh = core.snapshot(variant, target)
            expected = cycle._reviewed_fields(claimed["snapshot"])
            expected.update(title=checked["title"], body=checked["body"])
            if (record["slot"] != int(now() // 3600) or fingerprint(cycle._policy()) != fingerprint(policy)
                    or old_path.read_bytes() != old_bytes or source_data(session, variant) != source
                    or fresh["revision"] != claimed["snapshot"]["revision"] + 1
                    or cycle._reviewed_fields(fresh) != expected
                    or fresh["metadata"].get("postiz_state") != "DRAFT"
                    or fresh["metadata"].get("postiz_post_id") != captured["post_id"]
                    or fresh["metadata"].get("postiz_hash") != core.bridge.normalize(remote_after)[3]
                    or variant.config_hash != core.settings.hash or variant.binding_hash != target.binding_hash
                    or hashlib.sha256(inside(core.settings.root, trace).read_bytes()).hexdigest()
                       != metadata["machine_review_evidence"]["trace_hash"]):
                raise AgentError("REVISION_CONFLICT", 409)
            draft = session.get(Draft, variant.draft_id)
            draft.current = {**draft.current, "title": checked["title"], "body": checked["body"]}
            draft.revision += 1
            session.add(DraftRevision(draft_id=draft.id, revision=draft.revision,
                payload=copy.deepcopy(draft.current), config_id=draft.config_id))
            history = copy.deepcopy(variant.metadata_json.get("automatic_correction_history", []))
            history.append({**copy.deepcopy(attempt), "state": "completed", "finished_at": now(),
                            "previous_metadata": copy.deepcopy(snapshot["metadata"]),
                            "previous_group": remote_before["group"], "current_group": remote_after["group"]})
            variant.metadata_json = {**variant.metadata_json, **metadata,
                "approval_mode": "automatic", "correction_blocked": False,
                "machine_text_hash": text_hash(checked["title"], checked["body"]),
                "machine_source_hash": fingerprint(source), "machine_reviewed_at": now(),
                "translation_hash": translated_hash, "quality": "explicit_correction_ministral_languagetool",
                "automatic_correction_history": history}
            variant.metadata_json.pop("translation_model", None)
            variant.revision += 1
            variant.source_revision = draft.revision
            variant.updated_at = now()
            core._history(session, variant, target, ACTOR)
            core.db.audit(session, ACTOR, "hourly_staged_correction_completed", ident,
                claim_id=claim_id, reason=reason, postiz_id=captured["post_id"], no_approval=True, no_dispatch=True,
                previous_content_hash=reviewed_hash, machine_review_evidence_hash=metadata["machine_review_evidence_hash"],
                previous_group=remote_before["group"], current_group=remote_after["group"])
            record.update(state="automatic_ready", correction_blocked=False, quality_trace=trace,
                translation_hash=translated_hash, prepared_hash=fingerprint(core.snapshot(variant, target)))
        attempt.update(state="completed", finished_at=now())
        record.pop("error", None)
        cycle._save(ledger)
        return cycle._view(record)
    except Exception as exc:
        code = exc.code if isinstance(exc, AgentError) else "ARTICLE_AUTOMATIC_CORRECTION_INTERRUPTED"
        attempt.update(state="failed", error=code, finished_at=now())
        record.update(state="needs_attention", correction_blocked=True, error=code)
        cycle._save(ledger)
        with core.db.session.begin() as session:
            core.db.audit(session, ACTOR, "hourly_staged_correction_failed", ident,
                          claim_id=claim_id, error=code, reason=reason, quality_trace=trace)
        if isinstance(exc, AgentError):
            raise
        raise AgentError(code, 503) from None
