# Исправление подписей с журналом и подтверждением после перезагрузки.
"""Durable in-place caption corrections; an unconfirmed save is never repeated."""

import json
import time

from sqlalchemy import update

from .browser_runtime import COMPOSER, ensure_channel, matching_posts, session
from .config import AgentError, Settings, fingerprint, inside
from .db import Approval, Channel, Database, Draft, DraftRevision, Intent


# Операция validate: исправление подписей с журналом и подтверждением после перезагрузки.
def validate(service, draft, body):
    from .editorial import validate_draft

    # Обрабатываем ветвь при условии: not isinstance(body, str) or not body.strip().
    if not isinstance(body, str) or not body.strip():
        raise AgentError("CAPTION_REQUIRED")
    image = draft.current.get("image", {})
    # Обрабатываем ветвь при условии: image and any((value not in body for value in (image['page_url'], image['license_url'], 'Автор: ' + image['….
    if image and any(value not in body for value in (
        image["page_url"], image["license_url"], "Автор: " + image["author"],
    )):
        raise AgentError("IMAGE_ATTRIBUTION_REQUIRED")
    values = {**draft.current, "body": body}
    candidate = {k: v for k, v in values.items() if k not in {"checks", "video", "image"}}
    materials = [m for m in service.db.export()["materials"] if m["story_id"] == draft.story_id]
    checks = validate_draft(candidate, materials, service.settings.channel(draft.channel_id),
                            allow_original=bool(image) or draft.current.get("payload", {}).get("origin") == "generated_video")
    # Обрабатываем ветвь при условии: set(checks) - {'EDITOR_REVIEW_REQUIRED'}.
    if set(checks) - {"EDITOR_REVIEW_REQUIRED"}:
        raise AgentError("QUALITY_GATE_BLOCKED")
    return {**values, "checks": checks}


# Исправляем существующую подпись под блокировкой отправок.
def correct_caption(service, intent_id, revision, body):
    from .image_schedule import lock

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with lock(service.settings.root, "run"):
        return _correct_caption(service, intent_id, revision, body)


# Проверяем ревизию и права, затем сохраняем подтверждённую правку.
def _correct_caption(service, intent_id, revision, body):
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as db:
        intent = db.get(Intent, intent_id)
        draft = db.get(Draft, intent.draft_id) if intent else None
        channel = db.get(Channel, intent.channel_id) if intent else None
        # Обрабатываем ветвь при условии: not intent or intent.state != 'confirmed' or (not draft) or (draft.state != 'published').
        if not intent or intent.state != "confirmed" or not draft or draft.state != "published":
            raise AgentError("PUBLICATION_NOT_CONFIRMED", 409)
        # Обрабатываем ветвь при условии: not channel or not channel.paused.
        if not channel or not channel.paused:
            raise AgentError("CORRECTION_CHANNEL_MUST_BE_PAUSED", 409)
        # Обрабатываем ветвь при условии: draft.current['body'] == body.
        if draft.current["body"] == body:
            return {"state": "already_corrected", "intent_id": intent_id}
        # Обрабатываем ветвь при условии: draft.revision != revision.
        if draft.revision != revision:
            raise AgentError("DRAFT_REVISION_CONFLICT", 409)
        replacement = validate(service, draft, body)
        payload = {"intent_id": intent_id, "revision": revision, "chat_id": channel.chat_id, "channel_id": channel.id,
                   "old_body": draft.current["body"], "body": body}
        draft_id, old_hash = draft.id, fingerprint(draft.current)
    result = service.publisher.call("correct-caption", **payload)
    # Обрабатываем ветвь при условии: result['state'] != 'confirmed'.
    if result["state"] != "confirmed":
        return {**result, "intent_id": intent_id}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as db:
        draft = db.get(Draft, draft_id)
        # Обрабатываем ветвь при условии: draft.revision != revision or fingerprint(draft.current) != old_hash.
        if draft.revision != revision or fingerprint(draft.current) != old_hash:
            raise AgentError("DRAFT_REVISION_CONFLICT", 409)
        draft.current, draft.revision = replacement, revision + 1
        db.get(Intent, intent_id).body_hash = fingerprint(body)
        db.execute(update(Approval).where(Approval.draft_id == draft_id).values(revoked=True))
        db.add(DraftRevision(draft_id=draft_id, revision=draft.revision, payload=replacement,
                             config_id=draft.config_id))
        db.add(Approval(draft_id=draft_id, revision=draft.revision, content_hash=fingerprint(replacement),
                        config_id=draft.config_id, actor="owner"))
        service.db.audit(db, "owner", "browser_caption_corrected", intent_id,
                         revision=draft.revision, proof=result.get("proof"))
    return {"state": "confirmed", "intent_id": intent_id, "revision": revision + 1}


# Повторно проверяем разрешение операции перед внешним действием.
def authorize(settings, payload):
    # Обрабатываем ветвь при условии: Settings.load(settings.root).hash != settings.hash.
    if Settings.load(settings.root).hash != settings.hash:
        raise AgentError("BROWSER_CONFIG_CHANGED")
    db = Database(settings)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with db.session() as s:
            intent = s.get(Intent, payload["intent_id"])
            draft = s.get(Draft, intent.draft_id) if intent else None
            channel = s.get(Channel, intent.channel_id) if intent else None
            # Обрабатываем ветвь при условии: not intent or intent.state != 'confirmed' or (not draft) or (draft.state != 'published') or (draft.revision….
            if (not intent or intent.state != "confirmed" or not draft or draft.state != "published"
                    or draft.revision != payload["revision"] or draft.current["body"] != payload["old_body"]
                    or not channel or not channel.paused or channel.chat_id != payload["chat_id"]):
                raise AgentError("BROWSER_CORRECTION_STATE_CHANGED")
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        db.engine.dispose()


# Операция edit: исправление подписей с журналом и подтверждением после перезагрузки.
def edit(settings, payload):
    from .browser_worker import ledger_path, save

    intent_id, revision = payload["intent_id"], payload["revision"]
    # Reuse strict request-ID validation before forming an audit path.
    ledger_path(settings, intent_id)
    # Обрабатываем ветвь при условии: not isinstance(revision, int) or revision < 1.
    if not isinstance(revision, int) or revision < 1:
        raise AgentError("DRAFT_REVISION_CONFLICT")
    path = inside(settings.root, f"data/browser-corrections/{intent_id}-{revision}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Принадлежность каналу не меняет старый отпечаток: незавершённые правки можно сверить.
    content_hash = fingerprint({key: value for key, value in payload.items() if key != "channel_id"})
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    # Обрабатываем ветвь при условии: saved and saved['content_hash'] != content_hash.
    if saved and saved["content_hash"] != content_hash:
        raise AgentError("BROWSER_CORRECTION_CONTENT_CHANGED")
    # Обрабатываем ветвь при условии: saved and saved['state'] == 'confirmed'.
    if saved and saved["state"] == "confirmed":
        return {"state": "confirmed", "proof": path.relative_to(settings.root).as_posix()}
    authorize(settings, payload)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        ensure_channel(page, settings, payload["chat_id"])
        old = page.get_by_role("main").get_by_text(payload["old_body"], exact=True)
        new = matching_posts(page, payload["body"])
        # Обрабатываем ветвь при условии: saved.
        if saved:
            # Read-only recovery after an interrupted save. Never click save twice.
            # Обрабатываем ветвь при условии: new.count() == 1 and old.count() == 0.
            if new.count() == 1 and old.count() == 0:
                saved["state"] = "confirmed"
                save(path, saved)
                return {"state": "confirmed", "proof": path.relative_to(settings.root).as_posix()}
            return {"state": "unknown", "proof": path.relative_to(settings.root).as_posix()}
        # Обрабатываем ветвь при условии: old.count() != 1 or new.count() or page.locator(COMPOSER).inner_text().strip().
        if old.count() != 1 or new.count() or page.locator(COMPOSER).inner_text().strip():
            raise AgentError("BROWSER_CORRECTION_POST_NOT_UNIQUE")
        before = page.get_by_role("listitem").count()
        media_count = matching_posts(page, payload["old_body"]).locator("img, video").count()
        # MAX can keep the top of an older caption behind its fixed header.
        old.evaluate("e => e.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(300)
        box = old.bounding_box()
        # Обрабатываем ветвь при условии: not box.
        if not box:
            raise AgentError("BROWSER_CORRECTION_POST_NOT_VISIBLE")
        old.click(button="right", position={"x": min(20, box["width"] / 2),
                                            "y": min(20, box["height"] / 2)})
        page.get_by_role("menu").get_by_text("Редактировать", exact=True).click()
        composer = page.locator(COMPOSER)
        composer.fill(payload["body"])
        # MAX's edit form updates its saved draft on a real keyboard input event.
        composer.press("Control+End")
        composer.press("Space")
        composer.press("Backspace")
        ensure_channel(page, settings, payload["chat_id"], navigate=False)
        authorize(settings, payload)
        state = {"state": "editing", "content_hash": content_hash, "intent_id": intent_id,
                 "channel_id": payload.get("channel_id"), "revision": revision, "started_at": time.time(), "before_count": before,
                 "media_count": media_count}
        save(path, state)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            page.get_by_role("button", name="Отправить сообщение", exact=True).click()
            page.get_by_role("main").get_by_text(payload["body"], exact=True).wait_for(state="visible")
            page.wait_for_timeout(5000)
            page.reload(wait_until="domcontentloaded")
            ensure_channel(page, settings, payload["chat_id"], navigate=False)
            new = matching_posts(page, payload["body"])
            # Обрабатываем ветвь при условии: new.count() != 1 or page.get_by_role('main').get_by_text(payload['old_body'], exact=True).count() or page.g….
            if (new.count() != 1 or page.get_by_role("main").get_by_text(payload["old_body"], exact=True).count()
                    or page.get_by_role("listitem").count() != before
                    or new.locator("img, video").count() != media_count):
                raise AgentError("BROWSER_CORRECTION_NOT_CONFIRMED")
            page.screenshot(path=str(path.with_suffix(".png")),
                            clip={"x": 480, "y": 0, "width": 800, "height": 900})
            state["state"] = "confirmed"
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            state["state"] = "unknown"
        save(path, state)
        return {"state": state["state"], "proof": path.relative_to(settings.root).as_posix()}
