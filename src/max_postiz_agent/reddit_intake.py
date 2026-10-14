"""Two explicit input paths; never download a Reddit page behind a submitted link."""
import copy
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .adapters import OpenCode
from .config import AgentError, fingerprint
from .db import Draft, DraftRevision, Material, Story, now, serialize, uid
from .hub_models import RedditSubmission, Variant
from .reddit import RedditQuality, text_hash


def canonical_reddit_url(value):
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or parsed.hostname not in
                {"reddit.com", "www.reddit.com", "old.reddit.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ValueError
        match = re.fullmatch(r"/r/([A-Za-z0-9_]{1,32})/comments/([a-z0-9]{3,16})(?:/[^?#]*)?", parsed.path)
        if not match:
            raise ValueError
        subreddit, ident = match.groups()
        return ident, f"https://www.reddit.com/r/{subreddit.lower()}/comments/{ident}/"
    except (ValueError, AttributeError) as exc:
        raise AgentError("REDDIT_POST_URL_REQUIRED") from exc


class RedditIntake:
    def __init__(self, core):
        self.core = core
        self.quality_factory = None

    def options(self):
        path = self.core.settings.root / "config/reddit_intake.json"
        if not path.exists():
            return {"processing_enabled": True, "stage_postiz": False, "mail_target_id": ""}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if (set(value) != {"processing_enabled", "stage_postiz", "mail_target_id"}
                    or type(value["processing_enabled"]) is not bool
                    or type(value["stage_postiz"]) is not bool
                    or not isinstance(value["mail_target_id"], str)):
                raise ValueError
            return value
        except (ValueError, TypeError, OSError) as exc:
            raise AgentError("REDDIT_INTAKE_CONFIG_INVALID") from exc

    def _target(self, s, ident):
        target = self.core._target(s, ident)
        if not target.enabled or target.platform != "max":
            raise AgentError("REDDIT_MAX_TARGET_REQUIRED")
        return target

    @staticmethod
    def _state(row):
        return "awaiting_text" if not row.original.strip() else (
            "queued" if row.rights_ref else "awaiting_rights")

    def submit(self, title, original, url, target_id, origin="manual", actor="editor"):
        ident, canonical = canonical_reddit_url(url)
        title, original = title.strip(), original.strip()
        if not title or len(title) > 500 or len(original) > 8500:
            raise AgentError("REDDIT_INPUT_SIZE_INVALID")
        # A supplied link alone is a bookmark, not permission to fetch its page.
        hashed = text_hash(original) if original else None
        if origin not in {"manual", "ifttt"}:
            raise AgentError("REDDIT_ORIGIN_INVALID")
        try:
            with self.core.db.session.begin() as s:
                self._target(s, target_id)
                duplicate = s.scalar(select(RedditSubmission).where(or_(
                    RedditSubmission.reddit_id == ident,
                    RedditSubmission.original_hash == hashed if hashed else False)))
                if duplicate:
                    return {"id": duplicate.id, "state": duplicate.state, "duplicate": True}
                row = RedditSubmission(reddit_id=ident, url=canonical, title=title,
                    original=original, original_hash=hashed, origin=origin, target_id=target_id)
                row.state = self._state(row)
                s.add(row)
                s.flush()
                self.core.db.audit(s, actor, "reddit_intake", row.id, origin=origin)
                return {"id": row.id, "state": row.state, "duplicate": False}
        except IntegrityError:
            # Unique indexes arbitrate concurrent manual/mail submissions.
            with self.core.db.session() as s:
                duplicate = s.scalar(select(RedditSubmission).where(or_(
                    RedditSubmission.reddit_id == ident,
                    RedditSubmission.original_hash == hashed if hashed else False)))
                if not duplicate:
                    raise AgentError("REDDIT_INTAKE_CONFLICT", 409) from None
                return {"id": duplicate.id, "state": duplicate.state, "duplicate": True}

    def rows(self):
        with self.core.db.session() as s:
            return [serialize(x) for x in s.scalars(select(RedditSubmission)
                .order_by(RedditSubmission.created_at.desc()).limit(100))]

    def edit(self, ident, title, original, actor):
        if not title.strip() or len(title) > 500 or not original.strip() or len(original) > 8500:
            raise AgentError("REDDIT_INPUT_SIZE_INVALID")
        try:
            with self.core.db.session.begin() as s:
                row = s.get(RedditSubmission, ident, with_for_update=True)
                if not row:
                    raise AgentError("REDDIT_INTAKE_NOT_FOUND", 404)
                if row.state not in {"awaiting_text", "awaiting_rights", "failed", "rejected", "queued"}:
                    raise AgentError("REDDIT_INTAKE_LOCKED", 409)
                row.title, row.original = title.strip(), original.strip()
                row.original_hash = text_hash(row.original)
                # Changing the supplied source invalidates its previous attestation.
                row.rights_ref, row.rights_actor, row.error = "", "", None
                row.state, row.updated_at = self._state(row), now()
                self.core.db.audit(s, actor, "reddit_source_edited", ident)
        except IntegrityError:
            raise AgentError("REDDIT_DUPLICATE_TEXT", 409) from None
        return {"state": row.state}

    def attest_rights(self, ident, evidence, expected_hash, actor="owner"):
        if actor != "owner":
            raise AgentError("ROLE_FORBIDDEN", 403)
        if not evidence.strip() or len(evidence) > 1000:
            raise AgentError("REDDIT_RIGHTS_EVIDENCE_REQUIRED")
        with self.core.db.session.begin() as s:
            row = s.get(RedditSubmission, ident, with_for_update=True)
            if not row:
                raise AgentError("REDDIT_INTAKE_NOT_FOUND", 404)
            if row.state not in {"awaiting_rights", "awaiting_text", "failed", "rejected"}:
                raise AgentError("REDDIT_INTAKE_LOCKED", 409)
            if expected_hash != fingerprint({"title": row.title, "original": row.original, "url": row.url}):
                raise AgentError("REVISION_CONFLICT", 409)
            row.rights_ref, row.rights_actor = evidence.strip(), actor
            row.state, row.error, row.updated_at = self._state(row), None, now()
            self.core.db.audit(s, actor, "reddit_source_rights", row.id,
                               source_hash=expected_hash)
            return {"state": row.state}

    def _quality(self):
        if self.quality_factory:
            return self.quality_factory()
        # Separate clients pin the independent model regardless of env model overrides.
        translator, reviewer = OpenCode(self.core.settings), OpenCode(self.core.settings)
        translator.model, reviewer.model = "proxmox/qwen35-9b", "proxmox/qwen25-coder-7b"
        quality = RedditQuality(translator, reviewer,
            prompt_dir=self.core.settings.root / "prompts")
        return quality

    def process_one(self):
        if not self.options()["processing_enabled"]:
            return False
        owner, stamp = uid(), now()
        with self.core.db.session.begin() as s:
            # An interrupted generation needs explicit inspection, not an invisible retry loop.
            s.execute(update(RedditSubmission).where(RedditSubmission.state == "processing",
                RedditSubmission.lease_until < stamp).values(state="needs_attention",
                    owner=None, error="REDDIT_PROCESSING_INTERRUPTED"))
            row = s.scalar(select(RedditSubmission).where(RedditSubmission.state == "queued")
                .order_by(RedditSubmission.created_at).with_for_update(skip_locked=True).limit(1))
            if not row:
                return False
            ident = row.id
            claimed = s.execute(update(RedditSubmission).where(RedditSubmission.id == ident,
                RedditSubmission.state == "queued").values(state="processing", owner=owner,
                    lease_until=stamp + 3600, updated_at=stamp))
            if not claimed.rowcount:
                return False
            source = serialize(row)
        quality = None
        try:
            quality = self._quality()
            prepared = quality.prepare({"id": source["reddit_id"], "title": source["title"],
                "selftext": source["original"], "is_self": True,
                "permalink": urlsplit(source["url"]).path})
            with self.core.db.session.begin() as s:
                row = s.get(RedditSubmission, ident, with_for_update=True)
                if row.owner != owner or row.state != "processing" or row.lease_until < now():
                    raise AgentError("REDDIT_LEASE_LOST", 409)
                t = self._target(s, row.target_id)
                if not row.rights_ref:
                    raise AgentError("REDDIT_RIGHTS_EVIDENCE_REQUIRED")
                row.translation_hash = text_hash(prepared["translation"])
                story = Story(title=row.title, text=row.original,
                              text_hash=fingerprint({"reddit_submission": row.id}))
                s.add(story)
                s.flush()
                material = Material(source_id="reddit_" + row.origin, external_id=row.reddit_id,
                    story_id=story.id, title=row.title, text=row.original, url=prepared["url"],
                    text_hash=row.original_hash, language="en")
                s.add(material)
                s.flush()
                payload = {"title": prepared["title"], "body": prepared["body"], "language": "ru",
                    "source_ids": [material.id], "decision": "review", "reason_codes": [],
                    "claims": [], "confidence": 1.0, "payload": {}}
                draft = Draft(channel_id=t.profile_id, story_id=story.id, current=payload,
                    config_id=self.core.settings.hash, state="reddit_review")
                s.add(draft)
                s.flush()
                s.add(DraftRevision(draft_id=draft.id, revision=1, payload=copy.deepcopy(payload),
                                    config_id=self.core.settings.hash))
                variant = Variant(draft_id=draft.id, target_id=t.id, title=prepared["title"],
                    body=prepared["body"], sources=[{"id": material.id, "url": material.url,
                    "source_id": material.source_id}], media=[], source_revision=1,
                    config_hash=self.core.settings.hash, binding_hash=t.binding_hash,
                    metadata_json={"origin": "reddit_intake", "submission_id": row.id,
                        "quality": "qwen_ministral_languagetool", "topic": prepared["topic"]})
                s.add(variant)
                s.flush()
                self.core._history(s, variant, t, "reddit_quality")
                row.variant_id, row.state = variant.id, "ready"
                row.owner, row.lease_until, row.error, row.updated_at = None, 0, None, now()
                variant_id = variant.id
                self.core.db.audit(s, "reddit_quality", "reddit_prepared", row.id,
                                   variant_id=variant.id)
            if self.options()["stage_postiz"]:
                try:
                    self.core.stage_remote(variant_id)
                except AgentError as exc:
                    # stage_remote tracks unknown outcomes. Never create another remote draft.
                    with self.core.db.session.begin() as s:
                        s.get(RedditSubmission, ident).error = exc.code
            return True
        except Exception as exc:
            code = exc.code if isinstance(exc, AgentError) else (
                "REDDIT_DUPLICATE_TRANSLATION" if isinstance(exc, IntegrityError)
                else "REDDIT_PROCESSING_FAILED")
            with self.core.db.session.begin() as s:
                s.execute(update(RedditSubmission).where(RedditSubmission.id == ident,
                    RedditSubmission.owner == owner).values(owner=None, lease_until=0,
                        state="rejected" if code in {"REDDIT_QUALITY_REJECTED", "REDDIT_CANDIDATE_EXCLUDED",
                        "REDDIT_DUPLICATE_TRANSLATION"} else "failed", error=code, updated_at=now()))
            return False
        finally:
            if quality:
                quality.close()
                if not self.quality_factory:
                    quality.translator.client.close()
                    quality.reviewer.client.close()

    def resume(self, ident):
        with self.core.db.session.begin() as s:
            row = s.get(RedditSubmission, ident, with_for_update=True)
            if not row:
                raise AgentError("REDDIT_INTAKE_NOT_FOUND", 404)
            if row.variant_id or row.state not in {"failed", "needs_attention"}:
                raise AgentError("REDDIT_INTAKE_LOCKED", 409)
            row.owner, row.lease_until, row.error = None, 0, None
            row.state = self._state(row)
            self.core.db.audit(s, "owner", "reddit_resume", row.id)
            return {"state": row.state}
