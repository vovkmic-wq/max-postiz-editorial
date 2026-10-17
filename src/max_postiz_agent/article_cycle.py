"""Owner-delegated article tests; preparation and exact-version approval are separate."""
import copy
import json
import math
import os
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from .adapters import OpenCode
from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside
from .db import Draft, DraftRevision, Material, Story, now, uid
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Variant
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
                        "owner_instruction", "min_gap_seconds", "max_per_day"}
            if (set(policy) != required or type(policy["enabled"]) is not bool
                    or not all(isinstance(policy[k], str) and policy[k].strip()
                               for k in ("target_id", "source_id", "rights_evidence", "owner_instruction"))
                    or not isinstance(policy["allowed_hosts"], list) or not policy["allowed_hosts"]
                    or any(not isinstance(x, str) or not x or x != x.lower()
                           or any(c in x for c in "/:@?# ") for x in policy["allowed_hosts"])
                    or type(policy["min_gap_seconds"]) is not int or policy["min_gap_seconds"] < 3600
                    or type(policy["max_per_day"]) is not int or not 1 <= policy["max_per_day"] <= 24):
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
                or article["published_at"] < now() - self.core.settings.channel(target.profile_id)["freshness_hours"] * 3600):
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

    def _quality(self):
        if self.quality_factory:
            return self.quality_factory()
        from .article_quality import ArticleQuality
        translator, reviewer = OpenCode(self.core.settings), OpenCode(self.core.settings)
        translator.model, reviewer.model = "proxmox/qwen35-9b", "proxmox/qwen25-coder-7b"
        return ArticleQuality(translator, reviewer, prompt_dir=self.core.settings.root / "prompts")

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

    def prepare(self, article):
        with profile_lock(self.directory / "cycle.lock"):
            policy = self._policy()
            source, target = self._source_target(policy)
            article = self._article(article, policy, source, target)
            self._clear_outstanding(target.id)
            self._cadence(policy, now(), check_gap=False)
            ledger, slot = self._load(), int(now() // 3600)
            if any(item["slot"] == slot for item in ledger["records"]):
                raise AgentError("ARTICLE_SLOT_ALREADY_CLAIMED", 409)
            if any(item["state"] not in {"published", "failed", "cancelled", "rejected"} for item in ledger["records"]):
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            original_hash = text_hash(article["original"])
            material_hash = fingerprint(" ".join(article["original"].casefold().split()))
            if any(item.get("url") == article["url"] or item.get("original_hash") == original_hash for item in ledger["records"]):
                raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            with self.core.db.session() as session:
                if session.scalar(select(Material).where(or_(Material.url == article["url"],
                        Material.text_hash.in_({original_hash, material_hash})))):
                    raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            record = {"id": uid(), "slot": slot, "state": "preparing", "claimed_at": now(),
                      "url": article["url"], "original_hash": original_hash, "policy_hash": fingerprint(policy),
                      "target_id": target.id, "source_id": source["id"], "source_published_at": article["published_at"]}
            ledger["records"].append(record)
            self._save(ledger)
            quality = None
            try:
                quality = self._quality()
                prepared = quality.prepare(article["title"], article["original"], article["url"])
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
                    variant = Variant(draft_id=draft.id, target_id=target.id, title=prepared["title"],
                        body=prepared["body"], sources=[{"id": material.id, "url": material.url, "source_id": source["id"]}],
                        media=[], source_revision=1, config_hash=self.core.settings.hash,
                        binding_hash=target.binding_hash, metadata_json={"origin": "hourly_article_test",
                            "record_id": record["id"], "rights_evidence": policy["rights_evidence"],
                            "quality": "qwen_ministral_languagetool", "topic": prepared["topic"],
                            "translation_hash": translation_hash})
                    session.add(variant)
                    session.flush()
                    self.core._history(session, variant, target, "article_quality")
                    record.update(variant_id=variant.id, translation_hash=translation_hash,
                                  prepared_hash=fingerprint(self.core.snapshot(variant, target)), state="prepared")
                    self.core.db.audit(session, "article_quality", "hourly_article_prepared", variant.id,
                        source_hash=original_hash, policy_hash=record["policy_hash"])
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                record.update(state="needs_attention", error=exc.code if isinstance(exc, AgentError) else "ARTICLE_PREPARATION_INTERRUPTED")
                self._save(ledger)
                raise
            finally:
                if quality:
                    quality.close()
                    if not self.quality_factory:
                        quality.translator.client.close()
                        quality.reviewer.client.close()

    def publish(self, ident, reviewed_hash, due_at=None):
        with profile_lock(self.directory / "cycle.lock"):
            policy, ledger = self._policy(), self._load()
            record = self._record(ledger, ident)
            _, target = self._source_target(policy)
            if record["state"] != "prepared" or record["policy_hash"] != fingerprint(policy):
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            if record["slot"] != int(now() // 3600):
                raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
            if record["source_published_at"] < now() - self.core.settings.channel(target.profile_id)["freshness_hours"] * 3600:
                raise AgentError("STALE_SOURCE", 409)
            view = self._view(record)
            if reviewed_hash != record["prepared_hash"] or view["content_hash"] != reviewed_hash:
                raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
            self._clear_outstanding(target.id)
            earliest = self._cadence(policy, now(), check_gap=False)
            due = max(now() + 120, earliest) if due_at is None else due_at
            if (type(due) not in {int, float} or not math.isfinite(due) or due < now() + 10
                    or due >= (record["slot"] + 2) * 3600):
                raise AgentError("ARTICLE_SCHEDULE_INVALID", 409)
            self._cadence(policy, due)
            with self.core.db.session() as session:
                if session.get(RemoteDraft, record["variant_id"]):
                    raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            record.update(state="staging", due_at=due)
            self._save(ledger)
            try:
                external = self.core.stage_remote(record["variant_id"], due_at=due)
                record.update(state="staged", postiz_id=external)
                self._save(ledger)
                self.core.bridge.sync(force=True)
                fresh = self._view(record)
                if fresh["body"] != view["body"] or fresh["title"] != view["title"]:
                    raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
                self._clear_outstanding(target.id)
                self._cadence(policy, due)
                record["state"] = "approving"
                self._save(ledger)
                self.core.pause_target(target.id, False, actor="hourly_article_policy")
                dispatch = self.core.approve_variant(record["variant_id"], fresh["revision"],
                    fresh["content_hash"], due, actor="hourly_article_policy")
                record.update(state="pending", dispatch_id=dispatch)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_policy", "hourly_article_delegated_approval", dispatch,
                        policy_hash=record["policy_hash"], owner_instruction=policy["owner_instruction"],
                        reviewed_hash=reviewed_hash, approved_hash=fresh["content_hash"], due_at=due)
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                record.update(state="needs_attention", error=exc.code if isinstance(exc, AgentError) else "ARTICLE_PUBLICATION_INTERRUPTED")
                self._save(ledger)
                # Preserve an already approved dispatch and its immutable schedule on interruption.
                with self.core.db.session() as session:
                    active = session.scalar(select(Dispatch).join(Variant).where(
                        Variant.target_id == target.id, Dispatch.state.in_(ACTIVE)))
                if not active:
                    self.core.pause_target(target.id, True, actor="hourly_article_policy")
                raise

    def status(self, ident=None):
        with profile_lock(self.directory / "cycle.lock"):
            self._policy(enabled=False)
            ledger = self._load()
            if not ledger["records"]:
                return {"state": "empty"}
            record = self._record(ledger, ident) if ident else ledger["records"][-1]
            if record.get("variant_id"):
                with self.core.db.session() as session:
                    delivery = session.scalar(select(Dispatch).where(Dispatch.variant_id == record["variant_id"])
                                              .order_by(Dispatch.due_at.desc()))
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
                            record["state"] = delivery.state
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
