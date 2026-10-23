"""One audited correction of an existing automatic DRAFT; never approve or send."""
import copy
import hashlib
import json
import math
import os
import re
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT
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
    from .article_sources import require_legacy_source
    require_legacy_source(record)
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
    if record.get("reference_contract", LEGACY_REFERENCE_CONTRACT) == REFERENCE_CONTRACT:
        attempt["reference_contract"] = REFERENCE_CONTRACT
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
    return _finish_correction(cycle, ledger, record, policy, captured, claimed, attempt,
                              correction, old_path, old_bytes, reviewed_hash, reason)


def _finish_correction(cycle, ledger, record, policy, captured, claimed, attempt,
                       correction, old_path, old_bytes, reviewed_hash, reason, *, saved_review=None,
                       recovery_profile="mur"):
    core = cycle.core
    snapshot, source = captured["snapshot"], captured["source"]
    operation, trace = attempt["id"], attempt["quality_trace"]
    ident, claim_id = record["id"], attempt["claim_id"]
    try:
        reference_contract = cycle._quality_reference_contract(operation)
        if attempt.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract:
            raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_INPUT_CHANGED", 409)
        remote_before = _remote(cycle, captured, snapshot["body"])
        prompt = core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
        prompt_hash = hashlib.sha256(prompt.read_text(encoding="utf-8").encode()).hexdigest()
        if saved_review is not None and prompt_hash != attempt["review_prompt_hash"]:
            raise AgentError("ARTICLE_AUTOMATIC_CORRECTION_INPUT_CHANGED", 409)
        quality = cycle._quality(operation, review_only=True)
        try:
            if (quality.review_rubric != RUBRIC or quality.review_prompt_hash != prompt_hash
                    or quality.reference_contract != reference_contract):
                raise AgentError("ARTICLE_AUTOMATIC_RUBRIC_REQUIRED", 409)
            if saved_review is None:
                checked = quality.check_candidate(correction["title"], correction["body"],
                                                   source["title"], source["body"], source["url"])
            else:
                recover = (quality.recover_roni_spelling if recovery_profile == "roni"
                           else quality.recover_scientific_spelling)
                checked = recover(correction["title"], correction["body"],
                    source["title"].strip(), source["body"].strip(), source["url"], raw_review=saved_review,
                    expected_review_prompt_hash=prompt_hash)
        finally:
            close_quality(quality, close_models=not cycle.quality_factory)
        if (checked.get("machine_review_status") != "passed" or checked.get("title") != correction["title"]
                or checked.get("url") != source["url"]
                or checked.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract
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
        if saved_review is not None and not _recovery_trace_current(cycle, attempt):
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
                    or saved_review is not None and not _recovery_trace_current(cycle, attempt)
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
                "translation_hash": translated_hash, "quality": ("explicit_correction_ministral_languagetool" if saved_review is None
                            else "saved_ministral_fresh_gates_languagetool_roni_recovery" if recovery_profile == "roni"
                            else "saved_ministral_fresh_gates_languagetool_scientific_recovery"),
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


def _recovery_trace_current(cycle, attempt):
    raw = inside(cycle.core.settings.root, attempt["previous_trace"]).read_bytes()
    return (hashlib.sha256(raw).hexdigest() == attempt["previous_trace_hash"]
            and raw == inside(cycle.core.settings.root, attempt["previous_trace_backup"]).read_bytes())


def _saved_spelling_review(cycle, attempt, correction, source, expected_sha, *, profile="mur"):
    """Read one complete failed candidate trace, never a detached positive reply."""
    from .article_quality import (article_review_context, multiscale_spelling_evidence,
                                  rubric_spelling, scientific_spelling_review)
    error = "ARTICLE_RONI_SPELLING_RECOVERY_TRACE_INVALID" if profile == "roni" else "ARTICLE_SPELLING_RECOVERY_TRACE_INVALID"
    path = attempt.get("quality_trace")
    if path != f"data/hourly-articles/quality-{attempt['id']}.local.jsonl":
        raise AgentError(error, 409)
    raw = inside(cycle.core.settings.root, path).read_bytes()
    if not 0 < len(raw) <= 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise AgentError(error, 409)
    try:
        # Контракт берётся из durable владельца trace, а не из полей самого
        # ответа. Старый V1 сохраняет прежние пять полей reviewer context.
        reference_contract = cycle._quality_reference_contract(attempt["id"])
        if attempt.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract:
            raise ValueError
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
        phases = ["candidate_started", "gate_passed", "review_started", "review_returned", "review_parsed",
                  "spelling_started", "spelling_returned", "spelling_adjudicated", "rejected"]
        if ([event.get("phase") for event in events] != phases
                or any(event.get("record_id") != attempt["id"] or event.get("attempt") != 1 for event in events)
                or any(type(event.get("at")) not in {int, float} or not math.isfinite(event["at"]) for event in events)
                or [event["at"] for event in events] != sorted(event["at"] for event in events)
                or any(type(attempt.get(key)) not in {int, float} or not math.isfinite(attempt[key])
                       for key in ("started_at", "finished_at"))
                or not attempt["started_at"] <= events[0]["at"] <= events[-1]["at"] <= attempt["finished_at"]):
            raise ValueError
        canonical_source = {**source, "title": source["title"].strip(), "body": source["body"].strip()}
        source_text = canonical_source["title"] + "\n\n" + canonical_source["body"]
        text = correction["title"] + "\n\n" + correction["body"]
        expected_count, expected_word = (2, "Ниньо") if profile == "roni" else (1, "многомасштабного")
        if profile == "roni":
            from .article_quality import RONI_SOURCE_TERM, roni_spelling_evidence
            if (source["url"] != "https://svs.gsfc.nasa.gov/5685/"
                    or not re.search(r"(?<![A-Za-z])" + re.escape(RONI_SOURCE_TERM) + r"(?![A-Za-z])", source_text)):
                raise ValueError
        instruction = (cycle.core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt").read_text(encoding="utf-8")
        prompt_hash = hashlib.sha256(instruction.encode()).hexdigest()
        if (events[0].get("mode") != "candidate" or events[0].get("candidate") != correction
                or events[0].get("candidate_raw", correction) != correction
                or events[0].get("source_untrusted") != canonical_source
                or events[0].get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract
                or events[1].get("characters") != len(text + "\n\nИсточник: " + source["url"]
                                                        + "\nПеревод и редактура с помощью нейросетей.")
                or events[2].get("context") != article_review_context(instruction, source_text,
                    text, RUBRIC, prompt_hash, reference_contract=reference_contract)):
            raise ValueError
        raw_review = events[3]["raw"]
        review = scientific_spelling_review(raw_review)
        if review != events[4].get("review"):
            raise ValueError
        matches = events[6]["matches"]
        if (not isinstance(matches, list) or len(matches) != expected_count
                or events[7].get("adjudicated") != [] or events[7].get("unresolved") != matches
                or events[8].get("mode") != "candidate" or events[8].get("error") != "ARTICLE_QUALITY_REJECTED"
                or events[8].get("feedback") != {"review_approved": True, "topic": review["topic"],
                                                   "review_issues": [], "spelling_matches": matches}):
            raise ValueError
        positions = []
        for match in matches:
            offset, length = match["offset"], match["length"]
            if (type(offset) is not int or type(length) is not int or offset < 0
                    or length != len(expected_word)
                    or text.encode("utf-16-le")[offset * 2:(offset + length) * 2].decode("utf-16-le") != expected_word
                    or match.get("type", {}).get("typeName") != "UnknownWord"
                    or match.get("rule", {}).get("id") != "MORFOLOGIK_RULE_RU_RU"
                    or match.get("rule", {}).get("issueType") != "misspelling"):
                raise ValueError
            resolved = (roni_spelling_evidence(source_text, text, match) if profile == "roni"
                        else multiscale_spelling_evidence(source_text, text, match))
            if resolved is None:
                raise ValueError
            positions.append(offset)
        if profile == "roni":
            title_end = len(correction["title"].encode("utf-16-le")) // 2
            body_start = len((correction["title"] + "\n\n").encode("utf-16-le")) // 2
            if (len(set(positions)) != 2 or sum(position < title_end for position in positions) != 1
                    or sum(position >= body_start for position in positions) != 1):
                raise ValueError
        adjudicated, unresolved = rubric_spelling(source_text, text, matches, RUBRIC)
        if unresolved or len(adjudicated) != expected_count:
            raise ValueError
        return raw_review, raw, prompt_hash
    except (AgentError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise AgentError(error, 409) from exc


def recover_staged_spelling(cycle, ident, correction, reviewed_hash, trace_sha256, reason):
    """The original MUR-only recovery remains separate and unchanged."""
    return _recover_staged_spelling(cycle, ident, correction, reviewed_hash, trace_sha256, reason)


def recover_staged_roni_spelling(cycle, ident, correction, reviewed_hash, trace_sha256, reason):
    """One separately claimed recovery for exact title/body RONI findings only."""
    return _recover_staged_spelling(cycle, ident, correction, reviewed_hash, trace_sha256, reason, profile="roni")


def _recover_staged_spelling(cycle, ident, correction, reviewed_hash, trace_sha256, reason, *, profile="mur"):
    cycle._validate_correction(correction)
    roni = profile == "roni"
    error = "ARTICLE_RONI_SPELLING_RECOVERY_NOT_ELIGIBLE" if roni else "ARTICLE_SPELLING_RECOVERY_NOT_ELIGIBLE"
    ledger_key = "automatic_roni_spelling_recoveries" if roni else "automatic_spelling_recoveries"
    namespace = "max-postiz:staged-roni-spelling-recovery:" if roni else "max-postiz:staged-spelling-recovery:"
    claim_field = "automatic_roni_spelling_recovery_claim" if roni else "automatic_spelling_recovery_claim"
    audit_action = "hourly_staged_roni_spelling_recovery_started" if roni else "hourly_staged_spelling_recovery_started"
    if (not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9-]{36}", ident)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                   for value in (reviewed_hash, trace_sha256))
            or not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000):
        raise AgentError(error, 409)
    correction = {key: value.strip() for key, value in correction.items()}
    with profile_lock(cycle.directory / "cycle.lock"):
        core, policy = cycle.core, cycle._policy()
        ledger = cycle._load()
        record = cycle._record(ledger, ident)
        from .article_sources import require_legacy_source
        require_legacy_source(record)
        claim = str(uuid5(NAMESPACE_URL, namespace + ident))
        with core.db.session() as session:
            if record.get(ledger_key) or session.get(Audit, claim):
                raise AgentError("ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED", 409)
            attempts = record.get("automatic_corrections", [])
            if (policy.get("approval_mode") != "automatic" or policy.get("review_rubric") != RUBRIC
                    or record.get("state") != "needs_attention" or record.get("error") != "ARTICLE_QUALITY_REJECTED"
                    or record.get("correction_blocked") is not True or not isinstance(attempts, list) or len(attempts) != 1):
                raise AgentError(error, 409)
            failed = attempts[0]
            if (failed.get("state") != "failed" or failed.get("error") != "ARTICLE_QUALITY_REJECTED"
                    or failed.get("mutation_started_at") is not None or failed.get("candidate_hash") != cycle._candidate_hash(correction)):
                raise AgentError(error, 409)
            captured = _capture(cycle, session, record, policy, "needs_attention")
            snapshot, source = captured["snapshot"], captured["source"]
            original_claim = str(uuid5(NAMESPACE_URL, "max-postiz:staged-automatic-correction:" + ident))
            audit = session.get(Audit, original_claim)
            if (reviewed_hash != fingerprint(snapshot) or failed.get("claim_id") != original_claim
                    or not audit or audit.actor != ACTOR or audit.action != STARTED or audit.entity != ident
                    or any(audit.details.get(key) != failed.get(key) for key in (
                        "id", "claim_id", "candidate_hash", "previous_content_hash", "previous_trace",
                        "previous_trace_hash", "previous_snapshot_backup", "previous_trace_backup", "quality_trace",
                        "policy_hash", "postiz_id", "reference_contract"))):
                raise AgentError(error, 409)
        try:
            before_path = inside(core.settings.root, failed["previous_snapshot_backup"])
            before = json.loads(before_path.read_text(encoding="utf-8"))
            previous = before["captured"]["snapshot"]
            expected = copy.deepcopy(previous)
            expected["revision"] += 1
            expected["metadata"].update(approval_mode="automatic_correction_pending",
                                        automatic_correction_claim=original_claim, correction_blocked=True)
            if (expected != snapshot or fingerprint(previous) != failed["previous_content_hash"]
                    or record.get("prepared_hash") != fingerprint(previous)
                    or before["record"].get("id") != ident or before["captured"]["source"] != source
                    or failed["policy_hash"] != fingerprint(policy) or failed["postiz_id"] != captured["post_id"]):
                raise ValueError
            old_path = inside(core.settings.root, failed["previous_trace"])
            old_bytes = old_path.read_bytes()
            if (hashlib.sha256(old_bytes).hexdigest() != failed["previous_trace_hash"]
                    or old_bytes != inside(core.settings.root, failed["previous_trace_backup"]).read_bytes()
                    or previous["metadata"]["machine_review_evidence"]["trace_hash"] != failed["previous_trace_hash"]):
                raise ValueError
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise AgentError(error, 409) from exc
        saved_review, failed_bytes, prompt_hash = _saved_spelling_review(cycle, failed, correction, source, trace_sha256,
                                                                       profile=profile)
        _remote(cycle, captured, snapshot["body"])
        operation = uid()
        trace = f"data/hourly-articles/quality-{operation}.local.jsonl"
        prefix = "roni-spelling-recovery" if roni else "spelling-recovery"
        backup = cycle.directory / f"{prefix}-{operation}.before.local.json"
        raw_backup = cycle.directory / f"{prefix}-{operation}.failed.local.jsonl"
        _save_private(backup, json.dumps({"record": copy.deepcopy(record), "captured": captured}, ensure_ascii=False).encode("utf-8"))
        _save_private(raw_backup, failed_bytes)
        recovery = {"id": operation, "claim_id": claim, "started_at": now(), "state": "checking",
            "reason": reason.strip(), "candidate_hash": cycle._candidate_hash(correction),
            "previous_content_hash": reviewed_hash, "previous_trace": failed["quality_trace"],
            "previous_trace_hash": trace_sha256, "previous_snapshot_backup": backup.relative_to(core.settings.root).as_posix(),
            "previous_trace_backup": raw_backup.relative_to(core.settings.root).as_posix(),
            "quality_trace": trace, "policy_hash": fingerprint(policy), "postiz_id": captured["post_id"],
            "review_prompt_hash": prompt_hash, "original_correction_claim": original_claim,
            "no_model_call": True, "kind": "source_bound_roni_spelling" if roni else "source_bound_scientific_spelling"}
        if record.get("reference_contract", LEGACY_REFERENCE_CONTRACT) == REFERENCE_CONTRACT:
            recovery["reference_contract"] = REFERENCE_CONTRACT
        with core.db.session.begin() as session:
            if fingerprint(_capture(cycle, session, record, policy, "needs_attention")) != fingerprint(captured):
                raise AgentError("REVISION_CONFLICT", 409)
            try:
                session.add(Audit(id=claim, actor=ACTOR, action=audit_action, entity=ident,
                                  details=copy.deepcopy(recovery)))
                variant = core._variant(session, snapshot["variant_id"], True)
                variant.metadata_json = {**variant.metadata_json, claim_field: claim}
                variant.revision += 1
                core._history(session, variant, session.get(Target, variant.target_id), ACTOR)
                session.flush()
                claimed = {**captured, "snapshot": copy.deepcopy(core.snapshot(variant, session.get(Target, variant.target_id)))}
            except IntegrityError as exc:
                raise AgentError("ARTICLE_SPELLING_RECOVERY_ALREADY_ATTEMPTED", 409) from exc
        record.update(state="automatic_correcting", **{ledger_key: [recovery]})
        cycle._save(ledger)
        return _finish_correction(cycle, ledger, record, policy, captured, claimed, recovery,
                                  correction, old_path, old_bytes, reviewed_hash, reason.strip(), saved_review=saved_review,
                                  recovery_profile=profile)
