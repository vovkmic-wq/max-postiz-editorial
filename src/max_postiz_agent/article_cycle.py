"""Owner-delegated article tests; preparation and exact-version approval are separate."""
import copy
import hashlib
import json
import math
import os
import re
from contextlib import ExitStack
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from .adapters import OpenCode
from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside
from .db import Draft, DraftRevision, Material, Story, now, uid
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
from .reddit import text_hash


class HourlyArticleCycle:
    """A private durable ledger stops interrupted operations instead of replaying them."""

    def __init__(self, core, quality_factory=None):
        self.core, self.quality_factory = core, quality_factory
        self.directory = inside(core.settings.root, "data/hourly-articles")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.directory / "ledger.local.json"

    def _policy(self, enabled=True):
        path = inside(self.core.settings.root, "data/hourly-article-policy.local.json")
        try:
            policy = json.loads(path.read_text(encoding="utf-8-sig"))
            required = {"enabled", "target_id", "source_id", "allowed_hosts", "rights_evidence",
                        "owner_instruction", "min_gap_seconds", "max_per_day", "max_source_age_hours"}
            mode = policy.get("approval_mode", "legacy")
            valid_keys = (set(policy) == required | {"approval_mode", "review_rubric"}
                          and policy.get("review_rubric") == "meaning-first-v1") if mode == "automatic" else (
                              set(policy) in (required, required | {"approval_mode"}))
            if (not valid_keys
                    or policy.get("approval_mode", "legacy") not in {"legacy", "human", "automatic"}
                    or type(policy["enabled"]) is not bool
                    or not all(isinstance(policy[k], str) and policy[k].strip()
                               for k in ("target_id", "source_id", "rights_evidence", "owner_instruction"))
                    or not isinstance(policy["allowed_hosts"], list) or not policy["allowed_hosts"]
                    or any(not isinstance(x, str) or not x or x != x.lower()
                           or any(c in x for c in "/:@?# ") for x in policy["allowed_hosts"])
                    or type(policy["min_gap_seconds"]) is not int or policy["min_gap_seconds"] < 3600
                    or type(policy["max_per_day"]) is not int or not 1 <= policy["max_per_day"] <= 24
                    or type(policy["max_source_age_hours"]) is not int
                    or not 72 <= policy["max_source_age_hours"] <= 8760):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AgentError("ARTICLE_POLICY_INVALID", 409) from exc
        if enabled and not policy["enabled"]:
            raise AgentError("ARTICLE_POLICY_DISABLED", 409)
        return policy

    def _load(self):
        try:
            value = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.exists() else {"records": []}
            if set(value) != {"records"} or not isinstance(value["records"], list):
                raise ValueError
            if any(not isinstance(x, dict) or not {"id", "slot", "state"} <= set(x) for x in value["records"]):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError("ARTICLE_LEDGER_INVALID", 409) from exc

    def _save(self, ledger):
        temporary = self.ledger_path.with_suffix(".new")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(ledger, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.ledger_path)
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _source_target(self, policy):
        source = self.core.settings.source(policy["source_id"])
        rights = source["rights"]
        if (not source["enabled"] or not rights["verified"] or not rights["allow_storage"]
                or not rights["allow_derivative"] or rights["evidence_ref"] != policy["rights_evidence"]):
            raise AgentError("ARTICLE_RIGHTS_REQUIRED", 409)
        with self.core.db.session() as session:
            target = self.core._target(session, policy["target_id"])
            if (not target.enabled or target.platform != "max" or target.route != "max_browser"
                    or not self.core.bridge.enabled or not target.options.get("postiz_integration_id")
                    or target.profile_id not in source["channel_ids"]):
                raise AgentError("ARTICLE_TARGET_INVALID", 409)
            profile = self.core.settings.channel(target.profile_id)
            if not profile["enabled"] or profile["publication"]["mode"] == "dry_run":
                raise AgentError("PROFILE_NOT_PUBLISHABLE", 409)
            return source, target

    def _article(self, article, policy, source, target):
        try:
            if set(article) != {"url", "title", "original", "published_at"}:
                raise ValueError
            parsed = urlsplit(article["url"])
            if (parsed.scheme != "https" or parsed.hostname not in policy["allowed_hosts"]
                    or parsed.username or parsed.password or parsed.port not in {None, 443}
                    or parsed.query or parsed.fragment or not parsed.path):
                raise ValueError
            if (not all(isinstance(article[k], str) and article[k].strip() for k in ("title", "original"))
                    or len(article["title"] + article["original"]) > 9000
                    or type(article["published_at"]) not in {int, float}
                    or not math.isfinite(article["published_at"])):
                raise ValueError
            url = urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AgentError("ARTICLE_INPUT_INVALID") from exc
        if (article["published_at"] > now() + 300
                or article["published_at"] < now() - policy["max_source_age_hours"] * 3600):
            raise AgentError("STALE_SOURCE", 409)
        return {**article, "url": url}

    def _clear_outstanding(self, target_id, own_variant=None):
        with self.core.db.session() as session:
            query = select(Dispatch).join(Variant).where(Variant.target_id == target_id,
                                                       Dispatch.state.in_(ACTIVE))
            if own_variant:
                query = query.where(Dispatch.variant_id != own_variant)
            if session.scalar(query) or session.scalar(select(BridgePost).where(
                    BridgePost.target_id == target_id, BridgePost.mutation_owner.is_not(None))):
                raise AgentError("ARTICLE_OUTSTANDING_DELIVERY", 409)

    def _cadence(self, policy, stamp, check_gap=True):
        with self.core.db.session() as session:
            attempts = list(session.scalars(select(DispatchAttempt).join(Dispatch).join(Variant).where(
                Variant.target_id == policy["target_id"], DispatchAttempt.result == "published")))
            # Include verified deliveries whose historical attempt row is unavailable.
            deliveries = list(session.scalars(select(Dispatch).join(Variant).where(
                Variant.target_id == policy["target_id"], Dispatch.state == "published")))
            known = {a.dispatch_id for a in attempts}
            if any(d.id not in known for d in deliveries):
                raise AgentError("ARTICLE_PUBLISH_TIME_UNKNOWN", 409)
        times = [a.created_at for a in attempts]
        earliest = max(times) + policy["min_gap_seconds"] + 1 if times else 0
        if check_gap and stamp < earliest:
            raise AgentError("ARTICLE_HOURLY_GAP", 409)
        zone = ZoneInfo("Europe/Moscow")
        day = datetime.fromtimestamp(stamp, zone).date()
        if sum(datetime.fromtimestamp(value, zone).date() == day for value in times) >= policy["max_per_day"]:
            raise AgentError("ARTICLE_DAILY_LIMIT", 409)
        return earliest

    def _trace_writer(self, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", record_id):
            raise AgentError("ARTICLE_RECORD_ID_INVALID")
        path = inside(self.core.settings.root, f"data/hourly-articles/quality-{record_id}.local.jsonl")

        def write_event(event):
            if not isinstance(event, dict):
                raise AgentError("ARTICLE_TRACE_INVALID")
            encoded = json.dumps({**event, "at": now(), "record_id": record_id}, ensure_ascii=False) + "\n"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())

        return write_event

    def _quality(self, record_id=None, review_only=False):
        if self.quality_factory:
            return self.quality_factory()
        from .article_quality import ArticleQuality
        from .translation import DedicatedTranslator, configured_translator
        with ExitStack() as cleanup:
            if review_only:
                from .human_review import CandidateOnlyTranslator
                translator = CandidateOnlyTranslator()
            else:
                translator = configured_translator(self.core.settings)
            if translator is not None:
                cleanup.callback(translator.client.close)
            policy_path = self.core.settings.root / "data/hourly-article-policy.local.json"
            mode = self._policy(enabled=False).get("approval_mode") if policy_path.exists() else "legacy"
            human_mode = mode == "human"
            if (mode in {"human", "automatic"} and not review_only and (not isinstance(translator, DedicatedTranslator)
                                                  or translator.model != "hy-mt2-7b-q8")):
                raise AgentError("HUMAN_HY_TRANSLATOR_REQUIRED", 409)
            if translator is None:
                translator = OpenCode(self.core.settings)
                translator.model = "proxmox/qwen35-9b"
                cleanup.callback(translator.client.close)
            reviewer = OpenCode(self.core.settings)
            cleanup.callback(reviewer.client.close)
            reviewer.model = "proxmox/qwen25-coder-7b"
            quality = ArticleQuality(translator, reviewer, prompt_dir=self.core.settings.root / "prompts",
                                     trace=self._trace_writer(record_id) if record_id else None,
                                     allow_human_rejection=human_mode,
                                     review_rubric="meaning-first-v1" if mode == "automatic" else "legacy")
            cleanup.pop_all()
            return quality

    @staticmethod
    def _quality_provenance(quality, operator_candidate=False):
        from .translation import DedicatedTranslator
        if operator_candidate:
            return {"quality": "operator_ministral_languagetool"}
        translator = getattr(quality, "translator", None)
        if isinstance(translator, DedicatedTranslator):
            return {"quality": "dedicated_translation_independent_review_languagetool",
                    "translation_model": translator.model,
                    "review_model": getattr(quality.reviewer, "model", None)}
        return {"quality": "qwen_ministral_languagetool"}

    def _assert_pre_draft(self, record):
        if any(record.get(key) for key in ("variant_id", "postiz_id", "dispatch_id", "due_at", "prepared_hash")):
            raise AgentError("ARTICLE_PREPARATION_HAS_EXTERNAL_STATE", 409)
        with self.core.db.session() as session:
            if (session.scalar(select(Story).where(Story.text_hash == fingerprint({"hourly_article": record["id"]})))
                    or any(v.metadata_json.get("record_id") == record["id"]
                           for v in session.scalars(select(Variant)))):
                raise AgentError("ARTICLE_PREPARATION_HAS_EXTERNAL_STATE", 409)

    def _unpublished_variant(self, session, record):
        variant = self.core._variant(session, record["variant_id"], True)
        target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
        if (variant.target_id != record["target_id"]
                or variant.metadata_json.get("origin") != "hourly_article_test"
                or variant.metadata_json.get("record_id") != record["id"]
                or session.get(RemoteDraft, variant.id)
                or session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
                or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
                or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id,
                                                              TargetApproval.revoked.is_(False)))
                or session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                                                          BridgePost.mutation_owner.is_not(None)))):
            raise AgentError("ARTICLE_PREPARED_HAS_EXTERNAL_STATE", 409)
        return variant, target

    def _record(self, ledger, ident):
        record = next((item for item in ledger["records"] if item["id"] == ident), None)
        if not record:
            raise AgentError("ARTICLE_RECORD_NOT_FOUND", 404)
        return record

    def _view(self, record):
        result = copy.deepcopy(record)
        if record.get("variant_id"):
            with self.core.db.session() as session:
                variant = self.core._variant(session, record["variant_id"])
                target = self.core._target(session, variant.target_id)
                result.update(title=variant.title, body=variant.body, revision=variant.revision,
                              content_hash=fingerprint(self.core.snapshot(variant, target)))
        return result

    def _snapshot(self, record):
        with self.core.db.session() as session:
            variant = self.core._variant(session, record["variant_id"])
            target = self.core._target(session, variant.target_id)
            return copy.deepcopy(self.core.snapshot(variant, target))

    @staticmethod
    def _reviewed_fields(snapshot):
        value = copy.deepcopy(snapshot)
        value.pop("revision")
        for key in ("postiz_post_id", "postiz_hash", "postiz_due_at", "postiz_state"):
            value["metadata"].pop(key, None)
        return value

    @staticmethod
    def _validate_correction(correction):
        if (not isinstance(correction, dict) or set(correction) != {"title", "body"}
                or any(not isinstance(v, str) or not v.strip() for v in correction.values())
                or len(correction["title"]) > 500 or len(correction["body"]) > 3500
                or "Источник:" in correction["body"]
                or "Перевод и редактура с помощью нейросетей." in correction["body"]):
            raise AgentError("ARTICLE_CORRECTION_INVALID")

    @staticmethod
    def _candidate_hash(candidate):
        return fingerprint({key: candidate[key].strip() for key in ("title", "body")})

    def _guard_candidate_replay(self, ledger, record, candidate, operation):
        """A persisted substantive refusal cannot be replaced by a fresh model sample."""
        candidate_hash = self._candidate_hash(candidate)
        supplied_hash = fingerprint(candidate)
        rejected = False
        for name in ("preparation_attempts", "correction_attempts"):
            attempts = record.get(name, [])
            if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                raise AgentError("ARTICLE_LEDGER_INVALID", 409)
            for attempt in attempts:
                # Infrastructure errors, including LanguageTool unavailability, are
                # retryable. Their state may say 'rejected'; only this code is final.
                if attempt.get("error") != "ARTICLE_QUALITY_REJECTED":
                    continue
                hashes = [attempt[key] for key in ("candidate_hash", "candidate_normalized_hash", "candidate_raw_hash")
                          if key in attempt]
                if not hashes:
                    # Historical automatic Qwen attempts have no explicit candidate hash.
                    if name == "correction_attempts" or attempt.get("operator_correction"):
                        raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                    continue
                if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in hashes):
                    raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                if candidate_hash in hashes or supplied_hash in hashes:
                    rejected = True
        if rejected:
            record.setdefault("preparation_actions", []).append({
                "at": now(), "action": "rejected_candidate_replay_blocked", "operation": operation,
                "candidate_normalized_hash": candidate_hash, "error": "ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"})
            self._save(ledger)
            with self.core.db.session.begin() as session:
                self.core.db.audit(session, "article_quality", "hourly_article_candidate_replay_blocked",
                    record["id"], operation=operation, candidate_normalized_hash=candidate_hash,
                    source_hash=record["original_hash"], error="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED")
            raise AgentError("ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED", 409)

    def prepare(self, article, retry_record=None, candidate=None):
        if candidate is not None:
            if not retry_record:
                raise AgentError("ARTICLE_CANDIDATE_RETRY_REQUIRED", 409)
            self._validate_correction(candidate)
        with profile_lock(self.directory / "cycle.lock"):
            policy = self._policy()
            source, target = self._source_target(policy)
            article = self._article(article, policy, source, target)
            self._clear_outstanding(target.id)
            self._cadence(policy, now(), check_gap=False)
            ledger, slot = self._load(), int(now() // 3600)
            original_hash = text_hash(article["original"])
            material_hash = fingerprint(" ".join(article["original"].casefold().split()))
            if retry_record:
                record = self._record(ledger, retry_record)
                if (record["state"] not in {"needs_attention", "rejected", "failed"}
                        or any(record.get(key) for key in ("variant_id", "postiz_id", "dispatch_id", "due_at", "prepared_hash"))):
                    raise AgentError("ARTICLE_RETRY_NOT_SAFE", 409)
                if record["slot"] != slot:
                    raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                if (record["url"] != article["url"] or record["original_hash"] != original_hash
                        or record["policy_hash"] != fingerprint(policy)
                        or record["target_id"] != target.id or record["source_id"] != source["id"]
                        or record["source_published_at"] != article["published_at"]
                        or (record.get("input_hash") and record["input_hash"] != fingerprint(article))):
                    raise AgentError("ARTICLE_RETRY_INPUT_MISMATCH", 409)
                try:
                    self._assert_pre_draft(record)
                except AgentError as exc:
                    raise AgentError("ARTICLE_RETRY_NOT_SAFE", 409) from exc
                if candidate is None and (record.get("error") == "ARTICLE_QUALITY_REJECTED" or any(
                        item.get("error") == "ARTICLE_QUALITY_REJECTED"
                        for item in record.get("preparation_attempts", []))):
                    raise AgentError("ARTICLE_CORRECTION_CANDIDATE_REQUIRED", 409)
            else:
                record = None
                if any(item["slot"] == slot or item.get("automatic_slot") == slot for item in ledger["records"]):
                    raise AgentError("ARTICLE_SLOT_ALREADY_CLAIMED", 409)
            other_records = [item for item in ledger["records"] if item is not record]
            if (policy.get("approval_mode") in {"human", "automatic"}
                    and sum(item["state"] in {"awaiting_human_review", "automatic_ready", "pending", "sending",
                                              "unknown", "awaiting_postiz"} for item in other_records) >= 24):
                raise AgentError("ARTICLE_AUTOMATIC_BACKLOG" if policy["approval_mode"] == "automatic"
                                 else "ARTICLE_HUMAN_REVIEW_BACKLOG", 409)
            if any(item["state"] not in {"published", "failed", "cancelled", "rejected", "awaiting_human_review"}
                   and not (item["state"] == "automatic_ready" and item.get("automatic_enrolled"))
                   for item in other_records):
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            if any(item.get("url") == article["url"] or item.get("original_hash") == original_hash for item in other_records):
                raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            with self.core.db.session() as session:
                if session.scalar(select(Material).where(or_(Material.url == article["url"],
                        Material.text_hash.in_({original_hash, material_hash})))):
                    raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            if record is None:
                record = {"id": uid(), "slot": slot, "state": "preparing", "claimed_at": now(),
                          "url": article["url"], "original_hash": original_hash, "policy_hash": fingerprint(policy),
                          "target_id": target.id, "source_id": source["id"], "source_published_at": article["published_at"],
                          "input_hash": fingerprint(article), "preparation_attempts": []}
                ledger["records"].append(record)
            elif "preparation_attempts" not in record:
                # Preserve older failed ledgers without inventing an unrecorded completion time.
                record["preparation_attempts"] = [{"number": 1, "started_at": record["claimed_at"],
                    "state": record["state"], "error": record.get("error"), "legacy_record": True}]
            if candidate is not None:
                self._guard_candidate_replay(ledger, record, candidate, "prepare_candidate")
            attempt = {"number": len(record["preparation_attempts"]) + 1,
                       "started_at": now(), "state": "preparing", "operator_retry": bool(retry_record)}
            if candidate is not None:
                attempt.update(candidate_hash=self._candidate_hash(candidate), operator_correction=True,
                               candidate_raw_hash=fingerprint(candidate))
            record["preparation_attempts"].append(attempt)
            record.update(state="preparing", input_hash=fingerprint(article))
            record["quality_trace"] = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            self._save(ledger)
            quality = None
            try:
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_operator" if retry_record else "article_quality",
                        "hourly_article_prepare_attempt", record["id"], attempt=attempt["number"],
                        operator_retry=bool(retry_record), source_hash=original_hash,
                        input_hash=record["input_hash"], policy_hash=record["policy_hash"],
                        candidate_hash=attempt.get("candidate_hash"), operator_correction=candidate is not None)
                quality = self._quality(record["id"])
                prepared = (quality.check_candidate(candidate["title"], candidate["body"], article["title"],
                    article["original"], article["url"]) if candidate is not None else
                    quality.prepare(article["title"], article["original"], article["url"]))
                if record["slot"] != int(now() // 3600):
                    raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                return self._store_prepared(article, prepared, record, ledger, policy, source, target,
                    self._quality_provenance(quality, operator_candidate=candidate is not None), attempt)
            except Exception as exc:
                record.update(state="needs_attention", error=exc.code if isinstance(exc, AgentError) else "ARTICLE_PREPARATION_INTERRUPTED")
                attempt.update(state="needs_attention", error=record["error"], finished_at=now())
                self._save(ledger)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "article_quality", "hourly_article_prepare_error", record["id"],
                        attempt=attempt["number"], error=record["error"])
                raise
            finally:
                if quality:
                    from .human_review import close_quality
                    close_quality(quality, close_models=not self.quality_factory)

    def _store_prepared(self, article, prepared, record, ledger, policy, source, target,
                        provenance, attempt=None, *, check_slot=True):
        attempt = attempt or {}
        original_hash = record["original_hash"]
        material_hash = fingerprint(" ".join(article["original"].casefold().split()))
        if prepared["url"] != article["url"]:
            raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
        translation_hash = text_hash(prepared["translation"])
        if any(item.get("translation_hash") == translation_hash for item in ledger["records"]):
            raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
        with self.core.db.session.begin() as session:
            duplicates = session.scalars(select(Variant).where(Variant.target_id == target.id))
            if any(v.metadata_json.get("translation_hash") == translation_hash for v in duplicates):
                raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
            story = Story(title=article["title"], text=article["original"],
                          text_hash=fingerprint({"hourly_article": record["id"]}))
            session.add(story)
            session.flush()
            material = Material(source_id=source["id"], external_id=article["url"], story_id=story.id,
                title=article["title"], text=article["original"], url=article["url"],
                text_hash=material_hash, published_at=article["published_at"], language="en")
            session.add(material)
            session.flush()
            payload = {"title": prepared["title"], "body": prepared["body"], "language": "ru",
                "source_ids": [material.id], "decision": "review", "reason_codes": [],
                "claims": [], "confidence": 1.0, "payload": {}}
            draft = Draft(channel_id=target.profile_id, story_id=story.id, current=payload,
                config_id=self.core.settings.hash, state="article_review")
            session.add(draft)
            session.flush()
            session.add(DraftRevision(draft_id=draft.id, revision=1, payload=copy.deepcopy(payload),
                                     config_id=self.core.settings.hash))
            from .human_review import candidate_body, machine_metadata, text_hash as machine_text_hash
            if policy.get("approval_mode") == "automatic":
                from .automatic_policy import machine_metadata
            variant = Variant(draft_id=draft.id, target_id=target.id, title=prepared["title"],
                body=prepared["body"], sources=[{"id": material.id, "url": material.url, "source_id": source["id"]}],
                media=[], source_revision=1, config_hash=self.core.settings.hash,
                binding_hash=target.binding_hash, metadata_json={"origin": "hourly_article_test",
                    "record_id": record["id"], "rights_evidence": policy["rights_evidence"],
                    **provenance,
                    "approval_mode": policy.get("approval_mode", "legacy"),
                    "policy_hash": record["policy_hash"],
                    "machine_text_hash": machine_text_hash(prepared["title"], prepared["body"]),
                    "machine_source_hash": fingerprint({"title": article["title"],
                        "body": article["original"], "url": article["url"]}),
                    "machine_reviewed_at": now(),
                    **machine_metadata(prepared, {"title": article["title"], "body": article["original"],
                        "url": article["url"]}, self.core.settings.root / record["quality_trace"]),
                    "operator_correction_hash": attempt.get("candidate_hash"), "topic": prepared["topic"],
                    "translation_hash": translation_hash})
            session.add(variant)
            session.flush()
            self.core._history(session, variant, target, "article_quality")
            # Generation and database work may cross the reserved hour.
            # Raising inside the transaction rolls back every generated entity.
            if check_slot and record["slot"] != int(now() // 3600):
                raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
            record.update(variant_id=variant.id, translation_hash=translation_hash,
                          prepared_hash=fingerprint(self.core.snapshot(variant, target)), state="prepared")
            self.core.db.audit(session, "article_quality", "hourly_article_prepared", variant.id,
                source_hash=original_hash, policy_hash=record["policy_hash"],
                candidate_hash=self._candidate_hash({"title": variant.title,
                    "body": candidate_body(variant, {"url": article["url"]})}),
                machine_review_status=variant.metadata_json.get("machine_review_status", "passed"),
                machine_review_evidence_hash=variant.metadata_json.get("machine_review_evidence_hash"),
                operator_correction=bool(attempt.get("operator_correction")))
        if policy.get("approval_mode") in {"human", "automatic"}:
            reviewed = self._snapshot(record)
            record["state"] = "staging_human_draft" if policy["approval_mode"] == "human" else "staging_automatic_draft"
            self._save(ledger)
            external = self.core.stage_remote(record["variant_id"])
            fresh = self._snapshot(record)
            if (self._reviewed_fields(fresh) != self._reviewed_fields(reviewed)
                    or fresh["revision"] != reviewed["revision"] + 1
                    or fresh["metadata"].get("postiz_state") != "DRAFT"):
                raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
            record.update(state="awaiting_human_review" if policy["approval_mode"] == "human" else "automatic_ready", postiz_id=external,
                          prepared_hash=fingerprint(fresh))
        if attempt:
            attempt.update(state=record["state"], finished_at=now())
        record.pop("error", None)
        self._save(ledger)
        return self._view(record)

    def stage_rejected(self, ident, article, *, restore_cancelled=False, reason=""):
        """Recover exact completed negative evidence, never re-run or approve the model."""
        from .rejected_trace import recover_rejected
        if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 500:
            raise AgentError("ARTICLE_RECOVERY_REASON_REQUIRED", 409)
        with profile_lock(self.directory / "cycle.lock"):
            policy, ledger = self._policy(), self._load()
            if policy.get("approval_mode") != "human":
                raise AgentError("ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE", 409)
            record = self._record(ledger, ident)
            if (record["state"] not in {"needs_attention", "rejected", "failed", "cancelled"}
                    or (record["state"] == "cancelled" and restore_cancelled is not True)):
                raise AgentError("ARTICLE_REJECTED_RECOVERY_NOT_SAFE", 409)
            self._assert_pre_draft(record)
            source, target = self._source_target(policy)
            article = self._article(article, policy, source, target)
            if (record.get("input_hash") != fingerprint(article)
                    or record.get("original_hash") != text_hash(article["original"])
                    or record.get("url") != article["url"]
                    or record.get("source_published_at") != article["published_at"]
                    or record.get("policy_hash") != fingerprint(policy)
                    or record.get("target_id") != target.id or record.get("source_id") != source["id"]):
                raise AgentError("ARTICLE_RETRY_INPUT_MISMATCH", 409)
            self._clear_outstanding(target.id)
            others = [r for r in ledger["records"] if r is not record]
            if sum(r["state"] == "awaiting_human_review" for r in others) >= 24:
                raise AgentError("ARTICLE_HUMAN_REVIEW_BACKLOG", 409)
            if any(r["state"] not in {"published", "failed", "cancelled", "rejected", "awaiting_human_review"}
                   for r in others):
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            if any(r.get("url") == article["url"] or r.get("original_hash") == record["original_hash"] for r in others):
                raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            material_hash = fingerprint(" ".join(article["original"].casefold().split()))
            with self.core.db.session() as session:
                if session.scalar(select(Material).where(or_(Material.url == article["url"],
                        Material.text_hash.in_({record["original_hash"], material_hash})))):
                    raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            if record.get("quality_trace") != expected:
                raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
            raw = inside(self.core.settings.root, expected).read_bytes()
            prepared = recover_rejected(record, article, raw)
            action = {"at": now(), "action": "operator_stage_rejected", "reason": reason.strip(),
                      "previous_state": record["state"], "previous_error": record.get("error"),
                      "policy_hash": fingerprint(policy), "trace_hash": hashlib.sha256(raw).hexdigest(),
                      "state": "staging", "no_new_inference": True}
            record.setdefault("preparation_actions", []).append(action)
            record["state"] = "recovering_rejected"
            self._save(ledger)
            try:
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_operator", "hourly_article_rejected_recovery_started",
                        ident, recovery_action=action)
                self._store_prepared(article, prepared, record, ledger, policy, source, target,
                    {"quality": "preserved_ministral_rejection_languagetool", "recovered_rejection": True},
                    check_slot=False)
                action.update(state="staged_for_human", finished_at=now(), variant_id=record["variant_id"])
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                error = exc.code if isinstance(exc, AgentError) else "ARTICLE_REJECTED_RECOVERY_INTERRUPTED"
                action.update(state="needs_attention", finished_at=now(), error=error)
                record.update(state="needs_attention", error=error)
                self._save(ledger)
                raise

    def cancel_preparation(self, ident, reason="Operator reconciled and abandoned pre-draft preparation"):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise AgentError("ARTICLE_CANCELLATION_REASON_INVALID")
        with profile_lock(self.directory / "cycle.lock"):
            ledger = self._load()
            record = self._record(ledger, ident)
            if record["state"] not in {"needs_attention", "preparing", "rejected", "failed"}:
                raise AgentError("ARTICLE_PREPARATION_NOT_CANCELLABLE", 409)
            self._assert_pre_draft(record)
            action = {"at": now(), "action": "operator_cancelled", "previous_state": record["state"],
                      "reason": reason.strip()}
            record.setdefault("preparation_actions", []).append(action)
            with self.core.db.session.begin() as session:
                self.core.db.audit(session, "hourly_article_operator", "hourly_article_cancel_preparation", record["id"],
                    policy_hash=record["policy_hash"], source_hash=record["original_hash"],
                    previous_state=record["state"], reason=reason.strip())
            record.update(state="cancelled", cancelled_at=action["at"])
            self._save(ledger)
            return self._view(record)

    def abandon_prepared(self, ident, reason="Operator rejected an unpublished local article draft"):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise AgentError("ARTICLE_CANCELLATION_REASON_INVALID")
        with profile_lock(self.directory / "cycle.lock"):
            ledger = self._load()
            record = self._record(ledger, ident)
            if (record["state"] not in {"prepared", "needs_attention", "rejected", "failed", "revising"}
                    or not record.get("variant_id")
                    or any(record.get(key) for key in ("postiz_id", "dispatch_id", "due_at"))):
                raise AgentError("ARTICLE_PREPARED_NOT_ABANDONABLE", 409)
            with self.core.db.session.begin() as session:
                variant, target = self._unpublished_variant(session, record)
                variant.state, variant.revision = "abandoned", variant.revision + 1
                session.get(Draft, variant.draft_id).state = "article_rejected"
                self.core._history(session, variant, target, "hourly_article_operator")
                self.core.db.audit(session, "hourly_article_operator", "hourly_article_abandon_prepared", variant.id,
                    record_id=record["id"], policy_hash=record["policy_hash"], reason=reason.strip())
            action = {"at": now(), "action": "operator_abandoned_prepared", "previous_state": record["state"],
                      "reason": reason.strip()}
            record.setdefault("preparation_actions", []).append(action)
            record.update(state="cancelled", cancelled_at=action["at"])
            self._save(ledger)
            return self._view(record)

    def revise_prepared(self, ident, correction):
        self._validate_correction(correction)
        with profile_lock(self.directory / "cycle.lock"):
            policy, ledger = self._policy(), self._load()
            record = self._record(ledger, ident)
            if policy.get("approval_mode") == "automatic" and record.get("postiz_id"):
                raise AgentError("ARTICLE_EDIT_IN_POSTIZ_REQUIRED", 409)
            if (record["state"] != "prepared" or not record.get("variant_id")
                    or record["policy_hash"] != fingerprint(policy)):
                raise AgentError("ARTICLE_CORRECTION_NOT_ALLOWED", 409)
            if record["slot"] != int(now() // 3600):
                raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
            self._source_target(policy)
            with self.core.db.session.begin() as session:
                variant, target = self._unpublished_variant(session, record)
                prior_hash = fingerprint(self.core.snapshot(variant, target))
                if prior_hash != record["prepared_hash"] or len(variant.sources) != 1:
                    raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
                material = session.get(Material, variant.sources[0]["id"])
                if (not material or material.url != record["url"] or material.source_id != record["source_id"]
                        or text_hash(material.text) != record["original_hash"]):
                    raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
                original_title, original = material.title, material.text
            self._guard_candidate_replay(ledger, record, correction, "revise_prepared")
            attempt = {"started_at": now(), "candidate_hash": self._candidate_hash(correction), "state": "checking",
                       "candidate_raw_hash": fingerprint(correction)}
            record.setdefault("correction_attempts", []).append(attempt)
            record.update(state="revising", correction_blocked=True)
            self._save(ledger)
            quality = None
            try:
                quality = self._quality(record["id"])
                checked = quality.check_candidate(correction["title"], correction["body"], original_title,
                                                  original, record["url"])
                if record["slot"] != int(now() // 3600):
                    raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                self._source_target(self._policy())
                if fingerprint(self._policy()) != record["policy_hash"] or checked["url"] != record["url"]:
                    raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
                translation_hash = text_hash(checked["translation"])
                if any(item is not record and item.get("translation_hash") == translation_hash for item in ledger["records"]):
                    raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
                with self.core.db.session.begin() as session:
                    variant, target = self._unpublished_variant(session, record)
                    if fingerprint(self.core.snapshot(variant, target)) != prior_hash:
                        raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
                    duplicates = session.scalars(select(Variant).where(Variant.target_id == target.id, Variant.id != variant.id))
                    if any(v.metadata_json.get("translation_hash") == translation_hash for v in duplicates):
                        raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
                    draft = session.get(Draft, variant.draft_id)
                    draft.current = {**draft.current, "title": checked["title"], "body": checked["body"]}
                    draft.revision += 1
                    draft.state, draft.config_id = "article_review", self.core.settings.hash
                    session.add(DraftRevision(draft_id=draft.id, revision=draft.revision,
                        payload=copy.deepcopy(draft.current), config_id=draft.config_id))
                    variant.title, variant.body = checked["title"], checked["body"]
                    variant.revision += 1
                    variant.source_revision, variant.config_hash = draft.revision, draft.config_id
                    variant.binding_hash, variant.state, variant.updated_at = target.binding_hash, "review_pending", now()
                    metadata = {**variant.metadata_json, "translation_hash": translation_hash,
                        "quality": "operator_ministral_languagetool",
                        "topic": checked["topic"], "editorial_correction_hash": attempt["candidate_hash"]}
                    metadata.pop("translation_model", None)
                    metadata.pop("review_model", None)
                    from .human_review import text_hash as machine_text_hash
                    metadata["machine_text_hash"] = machine_text_hash(variant.title, variant.body)
                    variant.metadata_json = metadata
                    self.core._history(session, variant, target, "hourly_article_operator")
                    if record["slot"] != int(now() // 3600):
                        raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                    record.update(prepared_hash=fingerprint(self.core.snapshot(variant, target)),
                                  translation_hash=translation_hash, state="prepared", correction_blocked=False)
                    self.core.db.audit(session, "hourly_article_operator", "hourly_article_revised_prepared", variant.id,
                        record_id=record["id"], previous_hash=prior_hash, prepared_hash=record["prepared_hash"],
                        candidate_hash=attempt["candidate_hash"], policy_hash=record["policy_hash"])
                attempt.update(state="accepted", finished_at=now())
                record.pop("error", None)
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                error = exc.code if isinstance(exc, AgentError) else "ARTICLE_CORRECTION_INTERRUPTED"
                attempt.update(state="rejected", error=error, finished_at=now())
                # Keep the original version intact, but block publication until an
                # explicit corrected candidate passes. A fresh operator revision can retry.
                record.update(state="prepared", correction_blocked=True, error=error)
                self._save(ledger)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_operator", "hourly_article_correction_error", ident,
                        error=error, candidate_hash=attempt["candidate_hash"])
                raise
            finally:
                if quality:
                    from .human_review import close_quality
                    close_quality(quality, close_models=not self.quality_factory)

    def publish(self, ident, reviewed_hash, due_at=None):
        """Only the explicit automatic policy may grant a policy approval."""
        with profile_lock(self.directory / "cycle.lock"):
            if self._policy().get("approval_mode") != "automatic":
                raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
            from .automatic_policy import publish
            ledger = self._load()
            return publish(self, self._record(ledger, ident), ledger, reviewed_hash, due_at)

    def migrate_automatic(self, ident, reason):
        from .automatic_migration import migrate
        return migrate(self, ident, reason)

    def status(self, ident=None):
        with profile_lock(self.directory / "cycle.lock"):
            self._policy(enabled=False)
            ledger = self._load()
            if not ledger["records"]:
                return {"state": "empty"}
            record = self._record(ledger, ident) if ident else ledger["records"][-1]
            if ident is None:
                by_variant = {item.get("variant_id"): item for item in ledger["records"] if item.get("variant_id")}
                with self.core.db.session() as session:
                    active = session.scalar(select(Dispatch).where(Dispatch.variant_id.in_(by_variant),
                        Dispatch.state.in_(ACTIVE)).order_by(
                            Dispatch.state.in_({"sending", "unknown"}).desc(), Dispatch.due_at.asc()))
                    if active:
                        record = by_variant[active.variant_id]
            if record.get("variant_id"):
                with self.core.db.session() as session:
                    delivery = session.scalar(select(Dispatch).where(Dispatch.variant_id == record["variant_id"])
                                              .order_by(Dispatch.state.in_(ACTIVE).desc(),
                                                        (Dispatch.state == "published").desc(), Dispatch.due_at.desc()))
                    link = session.scalar(select(BridgePost).where(BridgePost.variant_id == record["variant_id"]))
                    if delivery:
                        record.update(dispatch_id=delivery.id, dispatch_state=delivery.state,
                                      delivery_verified=delivery.verified, error=delivery.error)
                        if delivery.state in {"sending", "unknown"}:
                            record["state"] = delivery.state
                        elif delivery.state == "published" and delivery.verified:
                            record["state"] = "awaiting_postiz"
                            attempt = session.scalar(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == delivery.id,
                                DispatchAttempt.result == "published").order_by(DispatchAttempt.created_at.desc()))
                            record["published_at"] = attempt.created_at if attempt else now()
                        elif delivery.state == "published":
                            record["state"] = "published_unverified"
                        elif delivery.state in {"failed", "cancelled"}:
                            variant = session.get(Variant, record["variant_id"])
                            record["state"] = ("awaiting_human_review" if
                                variant.metadata_json.get("approval_mode") == "human" else delivery.state)
                        elif record["state"] not in {"needs_attention", "approving"}:
                            record["state"] = delivery.state
                    if link:
                        record["postiz_id"] = link.post_id
            if record["state"] == "awaiting_postiz":
                remote = self.core.postiz.get_post(record["postiz_id"])
                record["postiz_state"] = remote.get("state")
                if remote.get("state") == "PUBLISHED":
                    record["state"] = "published"
            if record["state"] in {"published", "failed", "cancelled"}:
                self._clear_outstanding(record["target_id"], record.get("variant_id"))
                self.core.pause_target(record["target_id"], True, actor="hourly_article_policy")
            self._save(ledger)
            return self._view(record)
