"""Postiz can request review and observe status; only the owner can approve MAX delivery."""
import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, unquote

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select, text, update

from .config import AgentError, fingerprint
from .db import Draft, DraftRevision, Story, now, uid
from .hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
from .mobile_hold import assert_item_unheld, item_is_held

PROVIDER = "max-browser"


class MaxBridge:
    def __init__(self, core):
        self.core = core
        self.key = os.getenv("MAX_POSTIZ_BRIDGE_KEY", "")
        self.next_poll = 0
        self.last_error = None

    @property
    def enabled(self):
        return bool(self.key and self.core.publishing["postiz"].get("browser_bridge_enabled"))

    def authorize_request(self, value):
        if not self.enabled or not value or not hmac.compare_digest(value, self.key):
            raise AgentError("BRIDGE_ACCESS_DENIED", 403)

    def _seal(self, data):
        raw = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True).encode()).decode().rstrip("=")
        signature = hmac.new(self.key.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return raw + "." + signature

    def _open(self, value, kind):
        try:
            raw, signature = value.rsplit(".", 1)
            expected = hmac.new(self.key.encode(), raw.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            if data.get("kind") != kind or data.get("expires", 0) < now():
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError):
            raise AgentError("BRIDGE_TICKET_INVALID", 403) from None

    def target(self, ident, destination=None):
        with self.core.db.session() as s:
            t = self.core._target(s, ident)
            if not t.enabled or t.route != "max_browser" or t.platform != "max":
                raise AgentError("BRIDGE_TARGET_UNAVAILABLE", 409)
            if destination is not None and t.destination != destination:
                raise AgentError("BRIDGE_TARGET_CHANGED", 409)
            return t

    def connect(self, target_id, state, verifier):
        t = self.target(target_id)
        access = self.core.publisher.inspect(t.destination)
        if not access.get("publish") or not access.get("live_verified"):
            raise AgentError("BRIDGE_BROWSER_NOT_VERIFIED", 409)
        code = self._seal({"kind": "connect", "target": t.id, "destination": t.destination,
                           "state": state, "verifier": verifier, "expires": now() + 300})
        return {"code": code}

    def exchange(self, code, verifier):
        data = self._open(code, "connect")
        if not hmac.compare_digest(data["verifier"], verifier):
            raise AgentError("BRIDGE_TICKET_INVALID", 403)
        t = self.target(data["target"], data["destination"])
        token = self._seal({"kind": "channel", "target": t.id, "destination": t.destination,
                            "expires": now() + 365 * 86400})
        return {"id": t.destination, "name": "MAX — " + self.core.settings.channel(t.profile_id)["name"],
                "accessToken": token, "refreshToken": "", "expiresIn": 365 * 86400,
                "picture": "", "username": t.destination}

    def channel(self, token):
        data = self._open(token, "channel")
        return self.target(data["target"], data["destination"])

    @staticmethod
    def normalize(remote):
        try:
            if "image" not in remote or "childrenPost" not in remote:
                raise AgentError("BRIDGE_POSTIZ_SCHEMA_REQUIRED", 409)
            if remote.get("intervalInDays") or remote["childrenPost"]:
                raise AgentError("BRIDGE_THREADS_OR_REPEAT_UNSUPPORTED", 409)
            images = remote["image"]
            if isinstance(images, str):
                images = json.loads(images)
            images = images or []
            if not isinstance(images, list) or len(images) > 1:
                raise AgentError("MAX_SINGLE_MEDIA_REQUIRED")
            raw = remote["content"]
            if not isinstance(raw, str) or not raw.strip():
                raise AgentError("BODY_REQUIRED")
            body = BeautifulSoup(raw, "html.parser").get_text("\n").strip()
            due = datetime.fromisoformat(remote["publishDate"].replace("Z", "+00:00")).timestamp()
            source = {"content": raw, "image": images, "publishDate": remote["publishDate"],
                      "state": remote["state"], "settings": remote.get("settings"),
                      "group": remote["group"], "integration_id": remote["integration"]["id"]}
            return body, images, due, fingerprint(source)
        except (TypeError, ValueError, KeyError):
            raise AgentError("BRIDGE_REMOTE_INVALID") from None

    def _media(self, images, target):
        result = []
        base = urlsplit(self.core.postiz.base)
        allowed = {(base.scheme, base.netloc)}
        public_origin = getattr(self.core.postiz, 'public_origin', '')
        if public_origin:
            public = urlsplit(public_origin)
            allowed.add((public.scheme, public.netloc))
        for item in images:
            value = item.get("path", "")
            parsed = urlsplit(value)
            if (parsed.scheme or parsed.netloc) and (parsed.scheme, parsed.netloc) not in allowed:
                raise AgentError("BRIDGE_MEDIA_ORIGIN_DENIED")
            decoded = unquote(parsed.path)
            if not decoded.startswith("/uploads/") or ".." in Path(decoded).parts or parsed.query or parsed.fragment:
                raise AgentError("BRIDGE_MEDIA_PATH_DENIED")
            url = base.scheme + "://" + base.netloc + parsed.path
            try:
                with self.core.postiz.client.stream("GET", url) as response:
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";")[0]
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}.get(mime)
                    if not suffix:
                        raise AgentError("MEDIA_TYPE_UNSUPPORTED")
                    folder = self.core.settings.root / self.core.settings.app["media_dir"]
                    folder.mkdir(parents=True, exist_ok=True)
                    temporary = folder / (uid() + ".part")
                    digest, size = hashlib.sha256(), 0
                    try:
                        with temporary.open("wb") as stream:
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > 100 * 1024 * 1024:
                                    raise AgentError("MEDIA_TOO_LARGE")
                                digest.update(chunk)
                                stream.write(chunk)
                        path = folder / (digest.hexdigest() + suffix)
                        if path.exists():
                            temporary.unlink()
                        else:
                            temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            except httpx.HTTPError:
                raise AgentError("BRIDGE_MEDIA_UNAVAILABLE", 503) from None
            result.append({"path": path.relative_to(self.core.settings.root).as_posix(),
                           "sha256": digest.hexdigest(), "mime": mime})
        return self.core.check_media(result, target)

    @staticmethod
    def _row_view(row):
        return {column.key: copy.deepcopy(getattr(row, column.key)) for column in row.__table__.columns}

    def capture_draft_sync(self, session, variant, target):
        """Capture local CAS before the explicit owner's read; never repair old metadata."""
        from .preapproval_retirement import assert_mutable
        assert_mutable(session, variant.id)
        draft = session.get(RemoteDraft, variant.id)
        locked_delivery = session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id,
            Dispatch.state.in_({"sending", "unknown", "scheduled", "published"})
            | Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0) | Dispatch.lease_until.is_(None)))
        if locked_delivery:
            raise AgentError("CANCEL_OR_RECONCILE_FIRST", 409)
        links = list(session.scalars(select(BridgePost).where(BridgePost.variant_id == variant.id)))
        if (draft is None or len(links) != 1 or draft.synced_revision != variant.revision
                or draft.remote_hash != fingerprint({"body": variant.body}) or draft.state != "draft"):
            raise AgentError("REMOTE_EDIT_CONFLICT", 409)
        link = links[0]
        duplicates = list(session.scalars(select(RemoteDraft).where(RemoteDraft.external_id == draft.external_id)))
        if (len(duplicates) != 1 or not draft.correlation or not draft.external_id
                or link.post_id != draft.external_id or link.target_id != target.id
                or variant.target_id != target.id or target.route != "max_browser" or target.platform != "max"
                or not target.enabled or link.integration_id != target.options.get("postiz_integration_id")
                or not link.active or link.error or link.remote_state != "DRAFT"
                or link.mutation_owner is not None or link.mutation_until != 0
                or variant.config_hash != self.core.settings.hash or variant.binding_hash != target.binding_hash):
            raise AgentError("REMOTE_EDIT_CONFLICT", 409)
        return {"variant_id": variant.id, "revision": variant.revision, "external_id": draft.external_id,
                "variant": self._row_view(variant), "target": self._row_view(target),
                "draft": self._row_view(draft), "bridge": self._row_view(link),
                "config_hash": self.core.settings.hash}

    def _check_draft_sync(self, session, target, expected):
        if not isinstance(expected, dict) or not expected.get("variant_id"):
            raise AgentError("REMOTE_EDIT_CONFLICT", 409)
        variant = self.core._variant(session, expected["variant_id"], True)
        current_target = session.scalar(select(Target).where(Target.id == variant.target_id)
                                        .with_for_update().execution_options(populate_existing=True))
        session.scalar(select(RemoteDraft).where(RemoteDraft.variant_id == variant.id)
                       .with_for_update().execution_options(populate_existing=True))
        session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id)
                       .with_for_update().execution_options(populate_existing=True))
        if (current_target is None or target.id != current_target.id
                or self.capture_draft_sync(session, variant, current_target) != expected):
            raise AgentError("REMOTE_EDIT_CONFLICT", 409)
        return variant, current_target

    @staticmethod
    def _remote_settings(remote):
        try:
            settings = remote.get("settings") or {}
            settings = json.loads(settings) if isinstance(settings, str) else settings
            if not isinstance(settings, dict):
                raise ValueError
            return settings
        except (ValueError, TypeError):
            raise AgentError("BRIDGE_REMOTE_INVALID", 409) from None

    def _check_draft_binding(self, remote, target, draft, link=None, *, require_draft=True):
        settings = self._remote_settings(remote)
        marked = "hub_variant_id" in settings or "hub_correlation" in settings
        if (remote.get("id") != draft.external_id or (require_draft and remote.get("state") != "DRAFT")
                or remote.get("integration", {}).get("id") != target.options.get("postiz_integration_id")
                or (link is not None and (link.target_id != target.id or link.group_id != remote.get("group")
                    or link.integration_id != remote.get("integration", {}).get("id")))
                or ("__type" in settings and settings["__type"] != PROVIDER)
                or (marked and (settings.get("hub_variant_id") != draft.variant_id
                                or settings.get("hub_correlation") != draft.correlation))):
            raise AgentError("BRIDGE_REMOTE_BINDING_CHANGED", 409)

    @staticmethod
    def _blocked_post(session, post_id):
        from .preapproval_retirement import mutation_blocked
        link = session.get(BridgePost, post_id)
        identifiers = [row.variant_id for row in session.scalars(
            select(RemoteDraft).where(RemoteDraft.external_id == post_id))]
        if link:
            identifiers.append(link.variant_id)
        return next((ident for ident in identifiers if mutation_blocked(session, ident)), None)

    @staticmethod
    def _tracked_draft(session, post_id, link=None):
        matches = list(session.scalars(select(RemoteDraft).where(RemoteDraft.external_id == post_id)))
        if len(matches) > 1 or (matches and link is not None and matches[0].variant_id != link.variant_id):
            raise AgentError("BRIDGE_REMOTE_BINDING_CHANGED", 409)
        return matches[0] if matches else None

    def _resolve_tracked(self, session, remote, target, link=None):
        """Native correlation markers are an identity claim, never owner-content fallback."""
        tracked = self._tracked_draft(session, remote.get("id"), link)
        settings = self._remote_settings(remote)
        marked = "hub_variant_id" in settings or "hub_correlation" in settings
        if marked:
            variant_id, correlation = settings.get("hub_variant_id"), settings.get("hub_correlation")
            if not isinstance(variant_id, str) or not variant_id or not isinstance(correlation, str) or not correlation:
                raise AgentError("BRIDGE_REMOTE_BINDING_CHANGED", 409)
            candidate = session.get(RemoteDraft, variant_id)
            if (candidate is None or candidate.correlation != correlation
                    or candidate.external_id not in {None, remote.get("id")}
                    or (tracked is not None and tracked.variant_id != candidate.variant_id)):
                raise AgentError("BRIDGE_REMOTE_BINDING_CHANGED", 409)
            tracked = candidate
        if tracked is not None:
            variant_target = session.scalar(select(Variant.target_id).where(Variant.id == tracked.variant_id))
            other = session.scalar(select(BridgePost).where(BridgePost.variant_id == tracked.variant_id))
            if (variant_target != target.id
                    or remote.get("integration", {}).get("id") != target.options.get("postiz_integration_id")
                    or (other is not None and (other.post_id != remote.get("id") or other.target_id != target.id))):
                raise AgentError("BRIDGE_REMOTE_BINDING_CHANGED", 409)
        return tracked

    def import_post(self, remote, target, *, expected_local=None):
        from .preapproval_retirement import mutation_blocked
        explicit = expected_local is not None
        with self.core.db.session() as s:
            blocked = self._blocked_post(s, remote.get("id"))
            if blocked:
                if explicit:
                    raise AgentError("PREAPPROVAL_RETIRED_OR_RECONCILING", 409)
                return blocked
            if not explicit:
                tracked = self._resolve_tracked(s, remote, target, s.get(BridgePost, remote.get("id")))
                if tracked is not None and mutation_blocked(s, tracked.variant_id):
                    return tracked.variant_id
        body, images, due, remote_hash = self.normalize(remote)
        with self.core.db.session() as s:
            link = s.get(BridgePost, remote["id"])
            if not explicit:
                self._resolve_tracked(s, remote, target, link)
            if not explicit and link and (link.target_id != target.id or link.integration_id != remote["integration"]["id"]):
                raise AgentError("BRIDGE_REMOTE_BINDING_CHANGED", 409)
            if explicit:
                try:
                    captured_target = SimpleNamespace(**expected_local["target"])
                    captured_draft = SimpleNamespace(**expected_local["draft"])
                    captured_bridge = SimpleNamespace(**expected_local["bridge"])
                except (KeyError, TypeError):
                    raise AgentError("REMOTE_EDIT_CONFLICT", 409) from None
                self._check_draft_binding(remote, captured_target, captured_draft, captured_bridge)
            elif link and (link.mutation_owner or link.remote_hash == remote_hash):
                return link.variant_id
        media = self._media(images, target)
        with self.core.db.session.begin() as s:
            # Discovery serializes per remote ID without reversing Variant -> Target locks.
            if self.core.db.engine.dialect.name == "postgresql":
                key = int(hashlib.sha256(remote["id"].encode()).hexdigest()[:15], 16)
                s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            else:
                s.execute(update(Target).where(Target.id == target.id).values(paused=Target.paused))
            link = s.get(BridgePost, remote["id"])
            if explicit:
                v, target = self._check_draft_sync(s, target, expected_local)
                link = s.get(BridgePost, remote["id"])
                tracked = s.get(RemoteDraft, v.id)
                self._check_draft_binding(remote, target, tracked, link)
                resolved = self._resolve_tracked(s, remote, target, link)
                if link is not None and link.remote_hash == remote_hash:
                    if body != v.body or media != v.media:
                        raise AgentError("BRIDGE_REMOTE_CHANGED", 409)
                    return {"state": "unchanged", "revision": v.revision}
            else:
                resolved = self._resolve_tracked(s, remote, target, link)
            blocked = self._blocked_post(s, remote["id"])
            if blocked:
                return blocked
            if link:
                v = self.core._variant(s, link.variant_id, True)
                if not explicit and (link.mutation_owner or link.remote_hash == remote_hash):
                    return v.id
                published = s.scalar(select(Dispatch).where(Dispatch.variant_id == v.id, Dispatch.state == "published"))
                if published:
                    if explicit:
                        raise AgentError("CANCEL_OR_RECONCILE_FIRST", 409)
                    link.remote_state = remote["state"]
                    return v.id
                if self._preclick_error_state(s, link, v, target, remote, body, media, due, remote_hash):
                    return v.id
                try:
                    self.core._invalidate(s, v)
                except AgentError:
                    if explicit:
                        raise
                    link.error = "REMOTE_EDIT_DURING_SEND"
                    return v.id
                v.body, v.media = body, media
                v.revision += 1
                v.state = "review_pending"
            else:
                # Preserve source evidence for a draft originally exported by this hub.
                tracked = resolved
                if tracked and tracked.state in {"creating", "unknown", "draft"}:
                    if mutation_blocked(s, tracked.variant_id):
                        return tracked.variant_id
                    v = self.core._variant(s, tracked.variant_id, True)
                    if v.target_id != target.id:
                        raise AgentError("BRIDGE_TARGET_CHANGED")
                    # Correlation fields are optional on the public API; foreign values are never adopted.
                    if tracked.external_id is None:
                        tracked.external_id = remote["id"]
                    self._check_draft_binding(remote, target, tracked, require_draft=False)
                    tracked.state = "draft"
                    self.core._invalidate(s, v)
                    v.body, v.media, v.revision = body, media, v.revision + 1
                else:
                    story = Story(text_hash=fingerprint({"postiz": remote["id"]}), title=body.splitlines()[0][:120], text=body)
                    s.add(story)
                    s.flush()
                    current = {"title": story.title, "body": body, "language": "ru", "source_ids": [],
                               "decision": "review", "reason_codes": ["POSTIZ_OWNER_CONTENT"], "claims": [],
                               "confidence": 0, "payload": {}}
                    draft = Draft(channel_id=target.profile_id, story_id=story.id, current=current,
                                  config_id=self.core.settings.hash, state="review_pending")
                    s.add(draft)
                    s.flush()
                    s.add(DraftRevision(draft_id=draft.id, revision=1, payload=current, config_id=draft.config_id))
                    v = Variant(draft_id=draft.id, target_id=target.id, title=story.title, body=body,
                                media=media, sources=[], source_revision=1, config_hash=draft.config_id,
                                binding_hash=target.binding_hash, metadata_json={"origin": "postiz_max_bridge"})
                    s.add(v)
                    s.flush()
                link = BridgePost(post_id=remote["id"], integration_id=remote["integration"]["id"],
                                  group_id=remote["group"], target_id=target.id, variant_id=v.id)
                s.add(link)
            if v.metadata_json.get("origin") == "hourly_article_test":
                title = next((line.strip() for line in body.splitlines() if line.strip()), "")
                if not title:
                    raise AgentError("BODY_REQUIRED", 409)
                if len(title) > 500:
                    raise AgentError("ARTICLE_TITLE_TOO_LONG", 409)
                v.title = title
            link.remote_hash, link.remote_state, link.due_at = remote_hash, remote["state"], due
            link.group_id = remote["group"]
            link.active, link.error, link.updated_at = True, None, now()
            v.metadata_json = {**v.metadata_json, "postiz_post_id": link.post_id,
                               "postiz_hash": remote_hash, "postiz_due_at": due, "postiz_state": remote["state"]}
            tracked = s.get(RemoteDraft, v.id)
            if tracked is not None and tracked.external_id == link.post_id:
                # Plaintext hash and imported revision describe the very same atomic snapshot.
                # Existing same-hash imports return above: historical stale metadata is not migrated.
                self._check_draft_binding(remote, target, tracked, link, require_draft=explicit)
                tracked.synced_revision = v.revision
                tracked.remote_hash = fingerprint({"body": body})
                tracked.updated_at = now()
            self.core._history(s, v, target, "postiz_bridge")
            self.core.db.audit(s, "postiz_bridge", "bridge_import", v.id, post_id=link.post_id)
            return {"state": "updated", "revision": v.revision} if explicit else v.id

    def _preclick_error_state(self, session, link, variant, target, remote, body, media, due, remote_hash):
        """Postiz ERROR меняет только состояние при новом exact pre-click proof.

        Обычный импорт меняет revision и отзывает approval. Здесь исходные QA,
        snapshot и failed Attempt нужны native-закрытию; права на новый send нет.
        """
        if remote.get("state") != "ERROR":
            return False
        from .browser_publisher import read_preclick_failure
        from .hub import ACTIVE

        target = session.scalar(select(Target).where(Target.id == target.id).with_for_update())
        dispatches = list(session.scalars(select(Dispatch).where(
            Dispatch.variant_id == variant.id).with_for_update()))
        if len(dispatches) != 1 or target is None:
            return False
        dispatch = dispatches[0]
        approvals = list(session.scalars(select(TargetApproval).where(
            TargetApproval.variant_id == variant.id).with_for_update()))
        attempts = list(session.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id)))
        snapshot = dispatch.snapshot
        metadata = snapshot.get("metadata", {}) if isinstance(snapshot, dict) else {}
        previous_remote = {**remote, "state": "QUEUE"}
        if (len(approvals) != 1 or len(attempts) != 1 or not isinstance(metadata, dict)
                or dispatch.state != "failed" or dispatch.error != "BROWSER_PRECLICK_FAILED"
                or dispatch.attempts != 1 or dispatch.owner is not None or dispatch.lease_until != 0
                or dispatch.verified or dispatch.external_id is not None or dispatch.permalink is not None
                or attempts[0].result != "failed" or attempts[0].error != "BROWSER_PRECLICK_FAILED"
                or approvals[0].id != dispatch.approval_id or approvals[0].revoked
                or approvals[0].actor != "hourly_article_policy" or approvals[0].revision != dispatch.revision
                or approvals[0].scheduled_at != dispatch.due_at or approvals[0].snapshot_hash != dispatch.snapshot_hash
                or snapshot != self.core.snapshot(variant, target)
                or dispatch.snapshot_hash != fingerprint({"content": snapshot, "due_at": dispatch.due_at})
                or variant.config_hash != self.core.settings.hash or variant.binding_hash != target.binding_hash
                or target.platform != "max" or target.route != "max_browser" or not target.enabled
                or metadata.get("origin") != "hourly_article_test" or metadata.get("approval_mode") != "automatic"
                or metadata.get("reserve_contract") != "article-reserve-v1"
                or metadata.get("postiz_state") != "QUEUE"
                or metadata.get("postiz_hash") != self.normalize(previous_remote)[3]
                or link.remote_state != "QUEUE" or link.remote_hash != metadata.get("postiz_hash")
                or body != snapshot.get("body") or media != snapshot.get("media")
                or abs(due - dispatch.due_at) >= .001 or abs(link.due_at - due) >= .001
                or link.target_id != target.id or not link.active or link.error is not None
                or link.mutation_owner is not None or link.mutation_until != 0
                or remote.get("group") != link.group_id or remote.get("integration", {}).get("id") != link.integration_id
                or target.options.get("postiz_integration_id") != link.integration_id
                or remote.get("releaseId") not in (None, "") or remote.get("releaseURL") not in (None, "")
                or session.scalar(select(Dispatch).join(Variant).where(Variant.target_id == target.id,
                    Dispatch.id != dispatch.id, Dispatch.state.in_(ACTIVE) | Dispatch.owner.is_not(None)
                    | (Dispatch.lease_until != 0) | Dispatch.lease_until.is_(None)))):
            return False
        try:
            read_preclick_failure(self.core.settings, dispatch.id, snapshot=snapshot, chat_id=target.destination)
        except AgentError:
            return False
        link.remote_state, link.remote_hash, link.updated_at = "ERROR", remote_hash, now()
        self.core.db.audit(session, "postiz_bridge", "bridge_preclick_state_only", variant.id,
            post_id=link.post_id, dispatch_id=dispatch.id, snapshot_hash=dispatch.snapshot_hash,
            remote_hash=remote_hash, previous_state="QUEUE", state="ERROR")
        return True

    def sync(self, force=False, *, target_id=None):
        if not self.enabled or (not force and now() < self.next_poll):
            return 0
        self.next_poll = now() + 15
        with self.core.db.session() as s:
            targets = {t.options.get("postiz_integration_id"): t for t in s.scalars(select(Target))
                       if t.enabled and t.route == "max_browser" and t.options.get("postiz_integration_id")
                       and (target_id is None or t.id == target_id)}
        if not targets:
            return 0
        integrations = {i["id"]: i for i in self.core.postiz.integrations()}
        posts = self.core.postiz.posts()
        seen, count = set(), 0
        for remote in posts:
            ident = remote.get("integration", {}).get("id")
            if ident not in targets:
                continue
            seen.add(remote["id"])
            if (integrations.get(ident, {}).get("identifier") != PROVIDER or integrations[ident].get("disabled")
                    or integrations[ident].get("refreshNeeded") or integrations[ident].get("accountId") != targets[ident].destination):
                continue
            try:
                self.import_post(remote, targets[ident])
                count += 1
            except AgentError as exc:
                if exc.code == "PREAPPROVAL_RETIRED_OR_RECONCILING":
                    continue
                with self.core.db.session.begin() as s:
                    link = s.get(BridgePost, remote["id"])
                    if link and not self._blocked_post(s, remote["id"]):
                        link.error = exc.code
        with self.core.db.session.begin() as s:
            for link in s.scalars(select(BridgePost)):
                if target_id is not None and link.target_id != target_id:
                    continue
                if link.post_id not in seen and link.active:
                    try:
                        if self._blocked_post(s, link.post_id):
                            continue
                        v = self.core._variant(s, link.variant_id, True)
                        self.core._invalidate(s, v)
                    except AgentError as exc:
                        if exc.code == "PREAPPROVAL_RETIRED_OR_RECONCILING":
                            continue
                        link.error = "REMOTE_DELETE_DURING_SEND"
                    link.active = False
        self.last_error = None
        return count

    def attest_original(self, ident, revision, content_hash, actor):
        with self.core.db.session.begin() as s:
            v = self.core._variant(s, ident, True)
            t = self.core._target(s, v.target_id)
            if v.revision != revision or fingerprint(self.core.snapshot(v, t)) != content_hash:
                raise AgentError("REVISION_CONFLICT", 409)
            if v.sources or v.metadata_json.get("origin") != "postiz_max_bridge":
                raise AgentError("BRIDGE_ORIGINAL_ATTESTATION_NOT_APPLICABLE", 409)
            self.core._invalidate(s, v)
            v.metadata_json = {**v.metadata_json, "original_attested_hash": fingerprint({"body": v.body, "media": v.media})}
            v.revision += 1
            self.core._history(s, v, t, actor)
            self.core.db.audit(s, actor, "original_content_attested", v.id, revision=v.revision)
        return {"revision": v.revision}

    def check_approval(self, s, v, due):
        assert_item_unheld(s, v.id)
        link = s.scalar(select(BridgePost).where(BridgePost.variant_id == v.id))
        if not link:
            return
        if link.mutation_owner:
            raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
        if not link.active or link.error or link.remote_state != "QUEUE":
            raise AgentError("BRIDGE_POST_NOT_SCHEDULED", 409)
        if abs(due - link.due_at) > 0.001:
            raise AgentError("BRIDGE_SCHEDULE_MISMATCH", 409)

    def guard_send(self, dispatch, target):
        with self.core.db.session() as s:
            assert_item_unheld(s, dispatch.variant_id)
            link = s.scalar(select(BridgePost).where(BridgePost.variant_id == dispatch.variant_id))
        if not link:
            return
        if not self.enabled or not link.active or link.error or link.mutation_owner:
            raise AgentError("BRIDGE_SEND_BLOCKED", 409)
        remote = self.core.postiz.get_post(link.post_id)
        body, images, due, current = self.normalize(remote)
        integration = next((i for i in self.core.postiz.integrations() if i["id"] == link.integration_id), None)
        if (not integration or integration.get("disabled") or integration.get("refreshNeeded")
                or integration.get("identifier") != PROVIDER or integration.get("accountId") != target.destination):
            raise AgentError("BRIDGE_INTEGRATION_UNAVAILABLE", 409)
        if current != dispatch.snapshot["metadata"].get("postiz_hash") or remote["state"] != "QUEUE" or abs(due - dispatch.due_at) > 0.001:
            raise AgentError("BRIDGE_REMOTE_CHANGED", 409)
        if (dispatch.snapshot["metadata"].get("origin") == "hourly_article_test"
                and body != dispatch.snapshot["body"]):
            raise AgentError("BRIDGE_REMOTE_CHANGED", 409)
        if self._media(images, target) != dispatch.snapshot["media"]:
            raise AgentError("BRIDGE_REMOTE_MEDIA_CHANGED", 409)

    def submit(self, token, integration_id, post_id):
        t = self.channel(token)
        if t.options.get("postiz_integration_id") != integration_id:
            raise AgentError("BRIDGE_INTEGRATION_MISMATCH", 403)
        with self.core.db.session() as s:
            link = s.get(BridgePost, post_id)
            if self._blocked_post(s, post_id) or link is not None and item_is_held(s, link.variant_id):
                return self.status(token, integration_id, post_id)
        integration = next((i for i in self.core.postiz.integrations() if i["id"] == integration_id), None)
        if (not integration or integration.get("identifier") != PROVIDER
                or integration.get("accountId") != t.destination or integration.get("disabled") or integration.get("refreshNeeded")):
            raise AgentError("BRIDGE_INTEGRATION_UNAVAILABLE", 409)
        remote = self.core.postiz.get_post(post_id)
        if remote["integration"]["id"] != integration_id:
            raise AgentError("BRIDGE_INTEGRATION_MISMATCH", 403)
        self.import_post(remote, t)
        return self.status(token, integration_id, post_id)

    def status(self, token, integration_id, post_id):
        from .hub import ACTIVE
        t = self.channel(token)
        with self.core.db.session() as s:
            link = s.get(BridgePost, post_id)
            if not link or link.target_id != t.id or link.integration_id != integration_id:
                raise AgentError("BRIDGE_POST_NOT_FOUND", 404)
            if self._blocked_post(s, post_id):
                return {"state": "cancelled", "error": "PREAPPROVAL_RETIRED_OR_RECONCILING"}
            # A rescheduled dispatch may have an earlier date than its cancelled
            # predecessor. Poll the active attempt, then confirmed history.
            d = s.scalar(select(Dispatch).where(Dispatch.variant_id == link.variant_id)
                         .order_by(Dispatch.state.in_(ACTIVE).desc(),
                                   (Dispatch.state == "published").desc(), Dispatch.due_at.desc()))
            if d and d.state == "published" and d.verified:
                return {"state": "published", "external_id": d.external_id, "permalink": d.permalink}
            if d and d.state == "published":
                return {"state": "unverified", "error": "BRIDGE_DELIVERY_NOT_VERIFIED"}
            if not link.active and (not d or d.state not in {"sending", "unknown"}):
                return {"state": "cancelled", "error": "BRIDGE_REMOTE_DELETED"}
            if (not d or d.state == "pending") and item_is_held(s, link.variant_id):
                return {"state": "held", "paused": t.paused, "error": "MOBILE_ITEM_HELD"}
            return {"state": d.state if d else "review_pending", "paused": t.paused, "error": link.error or (d.error if d else None)}

    def mutation(self, identifiers, release=None, *, expected_hash=None, expected_post_id=None):
        compare = expected_hash is not None or expected_post_id is not None
        if compare and (release is not None or not isinstance(expected_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
                or not isinstance(expected_post_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", expected_post_id)
                or identifiers != [expected_post_id]):
            raise AgentError("BRIDGE_MUTATION_CAS_INVALID", 409)
        if not release:
            self.sync(force=True)
        owner = release or uid()
        with self.core.db.session.begin() as s:
            if not release:
                # Настройки и новое право редактора сериализуются одним fence.
                # Пауза не запрещает правку DRAFT; require_running здесь не нужен.
                from .server_models import EditorialControl
                from .server_state import CONTROL_ID, fence_editorial_configuration
                fence_editorial_configuration(s)
                s.scalar(select(EditorialControl).where(
                    EditorialControl.id == CONTROL_ID).with_for_update())
            links = list(s.scalars(select(BridgePost).where((BridgePost.post_id.in_(identifiers)) | (BridgePost.group_id.in_(identifiers))).order_by(BridgePost.variant_id)))
            if compare and (len(links) != 1 or links[0].post_id != expected_post_id):
                raise AgentError("BRIDGE_MUTATION_CAS_INVALID", 409)
            for link in links:
                if release:
                    # Releasing this exact owner must remain possible after a retirement claim.
                    s.scalar(select(Variant).where(Variant.id == link.variant_id).with_for_update())
                else:
                    self.core._variant(s, link.variant_id, True)
                    assert_item_unheld(s, link.variant_id)
                # Another native mutation may have committed while this call
                # waited for the variant lock. Do not reuse its earlier owner.
                s.refresh(link)
                active = s.scalar(select(Dispatch).where(Dispatch.variant_id == link.variant_id, Dispatch.state.in_({"sending", "unknown", "published"})).with_for_update())
                if not release and active:
                    raise AgentError("BRIDGE_DELIVERY_LOCKED", 409)
                if not release and link.mutation_owner and link.mutation_owner != owner:
                    raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
                if not release and compare and link.mutation_until != 0:
                    raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
                if release:
                    if link.mutation_owner == owner:
                        link.mutation_owner, link.mutation_until = None, 0
                else:
                    link.mutation_owner, link.mutation_until = owner, now() + 300
                    # Наблюдение устаревает, даже если lease успели снять до
                    # захвата строк операцией ручной блокировки.
                    link.updated_at = max(now(), math.nextafter(link.updated_at, math.inf))
        if compare:
            try:
                # The native guard owns this one committed lease. The fresh
                # read is deliberately after it, closing GET -> overwrite races.
                remote = self.core.postiz.get_post(expected_post_id)
                if (remote.get("id") != expected_post_id or remote.get("state") != "DRAFT"
                        or self.normalize(remote)[3] != expected_hash):
                    raise AgentError("BRIDGE_REMOTE_CHANGED", 409)
                with self.core.db.session() as s:
                    link = s.get(BridgePost, expected_post_id)
                    if not link or link.mutation_owner != owner:
                        raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
            except Exception:
                # The controller has not run: only a read happened. Release our
                # own lease; a changed/unknown other owner remains untouched.
                self.mutation([expected_post_id], release=owner)
                raise
            return {"owner": owner, "cas_verified": True,
                    "expected_hash": expected_hash, "expected_post_id": expected_post_id}
        return {"owner": owner}

    def unlock(self, post_id, confirm_no_request_inflight):
        if confirm_no_request_inflight is not True:
            raise AgentError("BRIDGE_OPERATOR_CONFIRMATION_REQUIRED")
        with self.core.db.session.begin() as s:
            link = s.get(BridgePost, post_id)
            if not link:
                raise AgentError("BRIDGE_POST_NOT_FOUND", 404)
            self.core._variant(s, link.variant_id, True)
            t = self.core._target(s, link.target_id)
            active = s.scalar(select(Dispatch).where(Dispatch.variant_id == link.variant_id, Dispatch.state.in_({"sending", "unknown"})))
            if not t.paused or active or link.mutation_until > now():
                raise AgentError("BRIDGE_UNLOCK_NOT_SAFE", 409)
            link.mutation_owner, link.mutation_until = None, 0
            self.core.db.audit(s, "owner", "bridge_mutation_released", link.post_id)
        self.sync(force=True)
        return {"state": "unlocked", "target_paused": True}
