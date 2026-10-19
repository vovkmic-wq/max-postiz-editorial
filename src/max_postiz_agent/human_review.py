"""Exact-text machine evidence for the human-reviewed hourly route."""
from sqlalchemy import select

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
        status = delivery.state if delivery else "awaiting_human_review" if current else "machine_recheck_required"
        return {"hourly_article": True, "human_review_required": True,
                "source_title": source["title"], "source_body": source["body"],
                "source_url": source["url"], "machine_review_current": current,
                "machine_review_status": variant.metadata_json.get(
                    "machine_review_status", "passed" if current else "unconfirmed"),
                "machine_review_issues": variant.metadata_json.get("machine_review_issues", []),
                "status": status, "revision": variant.revision,
                "content_hash": fingerprint(core.snapshot(variant, session.get(Target, variant.target_id)))}


def candidate_body(variant, source):
    body = variant.body
    prefix = variant.title + "\n\n"
    if body.startswith(prefix):
        body = body[len(prefix):]
    footer = "\n\nИсточник: " + source["url"]
    for suffix in (footer + "\nПеревод и редактура с помощью нейросетей.", footer):
        if body.endswith(suffix):
            return body[:-len(suffix)]
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
                                  Audit.action == "hourly_article_recheck_failed"))
        if any(row.details.get("error") == "ARTICLE_QUALITY_REJECTED"
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
                    "machine_review_status": "passed"}
        metadata.pop("translation_model", None)
        metadata.pop("review_model", None)
        variant.metadata_json = metadata
        core._history(session, variant, target, "machine_recheck")
        core.db.audit(session, "machine_recheck", "hourly_article_rechecked", ident,
                      revision=variant.revision, machine_text_hash=metadata["machine_text_hash"])
    return details(core, ident)
