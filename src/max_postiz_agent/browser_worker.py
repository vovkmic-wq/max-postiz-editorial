# Устойчивый журнал браузерной отправки и сверка неопределённых результатов.
"""One isolated browser operation. Durable intent is written before the send click."""

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

from .browser_runtime import COMPOSER, ensure_channel, matching_posts, session
from .config import AgentError, Settings, fingerprint, inside


def authorize_dispatch(settings, db, payload):
    """Check the hub outbox without consulting or enabling the legacy outbox."""
    from sqlalchemy import inspect
    from .db import now
    from .hub import HubService
    from .hub_models import Dispatch

    if not inspect(db.engine).has_table("hub_dispatches"):
        return False
    with db.session() as s:
        dispatch = s.get(Dispatch, payload["request_id"])
        if dispatch is None:
            return False
        if dispatch.state != "sending" or not dispatch.owner or dispatch.lease_until <= now() or dispatch.due_at > now():
            raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
        core = HubService(settings, db, bootstrap_targets=False)
        try:
            variant, target = core._valid_dispatch(s, dispatch)
            if (target.platform != "max" or target.route != "max_browser"
                    or target.destination != payload["chat_id"]
                    or dispatch.snapshot["body"] != payload["text"]
                    or fingerprint(dispatch.snapshot) != fingerprint(core.snapshot(variant, target))):
                raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
            media, attachment = dispatch.snapshot["media"], payload.get("attachment")
            if len(media) > 1 or bool(media) != bool(attachment):
                raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
            if media:
                expected = "browser-video" if media[0]["mime"] == "video/mp4" else "browser-image"
                if (attachment.get("type") != expected
                        or attachment.get("payload", {}).get("sha256") != media[0]["sha256"]
                        or attachment.get("payload", {}).get("path") != media[0]["path"]):
                    raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
            # Repeat the authoritative Postiz check immediately before the browser click.
            core.bridge.guard_send(dispatch, target)
            return True
        finally:
            core.close()


# Повторно проверяем разрешение операции перед внешним действием.
def authorize(settings, payload):
    from sqlalchemy import select
    from .db import Approval, Channel, Database, Draft, Intent

    # Обрабатываем ветвь при условии: Settings.load(settings.root).hash != settings.hash.
    if Settings.load(settings.root).hash != settings.hash:
        raise AgentError("BROWSER_CONFIG_CHANGED")
    db = Database(settings)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        if authorize_dispatch(settings, db, payload):
            return
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with db.session() as session_db:
            intent = session_db.get(Intent, payload["request_id"])
            # Обрабатываем ветвь при условии: not intent or intent.state != 'sending'.
            if not intent or intent.state != "sending":
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
            draft = session_db.get(Draft, intent.draft_id)
            channel = session_db.get(Channel, intent.channel_id)
            # Обрабатываем ветвь при условии: not draft or not channel or channel.paused or (channel.chat_id != payload['chat_id']).
            if not draft or not channel or channel.paused or channel.chat_id != payload["chat_id"]:
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
            profile = settings.channel(channel.id)
            approval = session_db.scalar(select(Approval).where(
                Approval.draft_id == draft.id, Approval.revision == draft.revision,
                Approval.revoked.is_(False), Approval.config_id == settings.hash,
                Approval.content_hash == fingerprint(draft.current),
            ))
            # Обрабатываем ветвь при условии: not approval or not profile['enabled'] or profile['publication']['mode'] != 'review' or (draft.current['bod….
            if not approval or not profile["enabled"] or profile["publication"]["mode"] != "review" or draft.current["body"] != payload["text"]:
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
            video, image, attachment = draft.current.get("video"), draft.current.get("image"), payload.get("attachment")
            media = video or image
            expected_type = "browser-video" if video else "browser-image"
            # Обрабатываем ветвь при условии: video and image or bool(media) != bool(attachment) or (media and (attachment.get('type') != expected_type o….
            if (video and image) or bool(media) != bool(attachment) or (media and (
                attachment.get("type") != expected_type
                or attachment.get("payload", {}).get("sha256") != media["sha256"]
                or attachment.get("payload", {}).get("path") != media["path"]
            )):
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        db.engine.dispose()


# Сохраняем журнал операции перед следующим внешним действием.
def save(path, data):
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


# Строим путь журнала по проверенному идентификатору попытки.
def ledger_path(settings, request_id):
    # Обрабатываем ветвь при условии: not re.fullmatch('[A-Za-z0-9_-]{1,128}', request_id).
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise AgentError("BROWSER_REQUEST_ID_INVALID")
    path = inside(settings.root, "data/browser-publications/" + request_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# Фиксируем неизменный файл вложения перед отправкой.
def snapshot_attachment(settings, attachment, request_id):
    # Обрабатываем ветвь при условии: not attachment.
    if not attachment:
        return None
    kind = attachment.get("type")
    # Обрабатываем ветвь при условии: kind not in {'browser-video', 'browser-image'}.
    if kind not in {"browser-video", "browser-image"}:
        raise AgentError("BROWSER_ATTACHMENT_INVALID")
    source = inside(settings.root, attachment["payload"]["path"])
    # Обрабатываем ветвь при условии: kind == 'browser-video'.
    if kind == "browser-video":
        # Обрабатываем ветвь при условии: source.suffix.lower() != '.mp4' or source.stat().st_size > settings.app.get('video', {}).get('max_bytes', 1….
        if source.suffix.lower() != ".mp4" or source.stat().st_size > settings.app.get("video", {}).get("max_bytes", 104857600):
            raise AgentError("BROWSER_ATTACHMENT_INVALID")
    else:
        # Альтернативная ветвь после проверки основного условия.
        from .media import inspect_image

        inspect_image(settings.root, attachment["payload"]["path"])
    target = ledger_path(settings, request_id).with_suffix(source.suffix.lower())
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with source.open("rb") as original, target.open("wb") as copy:
        shutil.copyfileobj(original, copy)
    # Обрабатываем ветвь при условии: hashlib.sha256(target.read_bytes()).hexdigest() != attachment['payload']['sha256'].
    if hashlib.sha256(target.read_bytes()).hexdigest() != attachment["payload"]["sha256"]:
        raise AgentError("VIDEO_HASH_CHANGED" if kind == "browser-video" else "IMAGE_HASH_CHANGED")
    return target


# Сохраняем проверенный снимок видео для устойчивой отправки.
def snapshot_video(settings, attachment, request_id):
    """Compatibility entrypoint for existing video checks."""
    # Обрабатываем ветвь при условии: attachment and attachment.get('type') != 'browser-video'.
    if attachment and attachment.get("type") != "browser-video":
        raise AgentError("BROWSER_ATTACHMENT_INVALID")
    return snapshot_attachment(settings, attachment, request_id)


# Проверяем признаки успешной операции в интерфейсе MAX.
def receipt(page, state):
    posts = matching_posts(page, state["text"])
    # Обрабатываем ветвь при условии: posts.count() != state['before_count'] + 1.
    if posts.count() != state["before_count"] + 1:
        return None
    selector = ('video, [aria-label^="Прикрепленные"]' if state.get("attachment_type") == "browser-video"
                else 'img[src^="blob:"], img[src*="static.max"], [aria-label^="Прикрепленные"]')
    # Обрабатываем ветвь при условии: state.get('attachment') and (not posts.last.locator(selector).count()).
    if state.get("attachment") and not posts.last.locator(selector).count():
        return None
    return {"state": "confirmed", "external_id": "browser-" + state["request_id"],
            "permalink": None}


# Проверяем доступность ресурса и его разрешённые свойства.
def inspect(settings, chat_id):
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        ensure_channel(page, settings, chat_id)
        return {"publish": True, "read": False, "edit": False, "remove": False,
                "create_channel": False, "live_verified": True, "transport": "browser"}


# Сверяем сохранённую попытку без повторного нажатия отправки.
def reconcile(settings, chat_id, request_id):
    path = ledger_path(settings, request_id)
    # Обрабатываем ветвь при условии: not path.exists().
    if not path.exists():
        return {"state": "unknown"}
    state = json.loads(path.read_text(encoding="utf-8"))
    # Обрабатываем ветвь при условии: state['chat_id'] != chat_id.
    if state["chat_id"] != chat_id:
        raise AgentError("BROWSER_WRONG_CHANNEL")
    # Обрабатываем ветвь при условии: state.get('state') == 'confirmed'.
    if state.get("state") == "confirmed":
        return state["delivery"]
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        ensure_channel(page, settings, chat_id)
        found = receipt(page, state)
        # Обрабатываем ветвь при условии: found.
        if found:
            page.screenshot(path=str(path.with_suffix(".png")))
            state.update(state="confirmed", delivery=found)
            save(path, state)
            return found
    return {"state": "unknown"}


# Выполняем отправку и возвращаем подтверждённый либо неопределённый результат.
def publish(settings, payload, *, authorize_before_click=None):
    request_id, chat_id, text = payload["request_id"], payload["chat_id"], payload["text"]
    # Обрабатываем ветвь при условии: not isinstance(text, str) or not text.strip() or len(text) > 4000.
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise AgentError("BROWSER_TEXT_INVALID")
    path = ledger_path(settings, request_id)
    content_hash = fingerprint(payload)
    # Обрабатываем ветвь при условии: path.exists().
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        # Обрабатываем ветвь при условии: state['content_hash'] != content_hash.
        if state["content_hash"] != content_hash:
            raise AgentError("BROWSER_INTENT_CONTENT_CHANGED")
        return state["delivery"] if state.get("state") == "confirmed" else {"state": "unknown"}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        ensure_channel(page, settings, chat_id)
        composer = page.locator(COMPOSER)
        # Обрабатываем ветвь при условии: composer.inner_text().strip().
        if composer.inner_text().strip():
            raise AgentError("BROWSER_COMPOSER_NOT_EMPTY")
        # Pending attachments belonging to a human are preserved as well.
        # Обрабатываем ветвь при условии: page.get_by_role('button', name='Отменить загрузку', exact=True).count().
        if page.get_by_role("button", name="Отменить загрузку", exact=True).count():
            raise AgentError("BROWSER_COMPOSER_NOT_EMPTY")
        before = matching_posts(page, text).count()
        upload = snapshot_attachment(settings, payload.get("attachment"), request_id)
        # Обрабатываем ветвь при условии: upload.
        if upload:
            page.get_by_role("button", name="Загрузить файл", exact=True).click()
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with page.expect_file_chooser() as chooser:
                page.get_by_text("Фото или видео", exact=True).click()
            chooser.value.set_files(str(upload))
        composer.fill(text)
        send = page.get_by_role("button", name="Отправить сообщение", exact=True)
        send.wait_for(state="visible")
        upload_deadline = time.monotonic() + 30
        # Продолжаем цикл, пока выполняется условие: not send.is_enabled() and time.monotonic() < upload_deadline.
        while not send.is_enabled() and time.monotonic() < upload_deadline:
            page.wait_for_timeout(200)
        # Обрабатываем ветвь при условии: not send.is_enabled().
        if not send.is_enabled():
            raise AgentError("BROWSER_ATTACHMENT_NOT_READY")
        ensure_channel(page, settings, chat_id, navigate=False)
        # Обрабатываем ветвь при условии: authorize_before_click.
        if authorize_before_click:
            authorize_before_click()
        state = {"state": "sending", "request_id": request_id, "chat_id": chat_id,
                 "text": text, "attachment": bool(upload),
                 "attachment_type": payload.get("attachment", {}).get("type") if upload else None,
                 "before_count": before,
                 "content_hash": content_hash, "started_at": time.time()}
        # An interrupted click must never allow another automatic click.
        save(path, state)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            send.click()
            deadline = time.monotonic() + settings.app["max"]["browser"].get("operation_timeout_seconds", 120)
            # Продолжаем цикл, пока выполняется условие: time.monotonic() < deadline.
            while time.monotonic() < deadline:
                # Обрабатываем ветвь при условии: matching_posts(page, text).count() == before + 1 and (not composer.inner_text().strip()).
                if matching_posts(page, text).count() == before + 1 and not composer.inner_text().strip():
                    # MAX first renders an optimistic post. Reloading immediately can
                    # abort its in-flight upload/send request and discard the post.
                    page.wait_for_timeout(5000)
                    # Обрабатываем ветвь при условии: matching_posts(page, text).count() != before + 1.
                    if matching_posts(page, text).count() != before + 1:
                        continue
                    page.screenshot(path=str(path.with_suffix(".before-reload.png")))
                    page.reload(wait_until="domcontentloaded")
                    ensure_channel(page, settings, chat_id, navigate=False)
                    found = receipt(page, state)
                    # Обрабатываем ветвь при условии: found.
                    if found:
                        page.screenshot(path=str(path.with_suffix(".png")))
                        state.update(state="confirmed", delivery=found)
                        save(path, state)
                        return found
                    state["error"] = "BROWSER_POST_DISAPPEARED_AFTER_RELOAD"
                    break
                page.wait_for_timeout(500)
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            state["error"] = "BROWSER_SEND_UNCONFIRMED"
        # Обрабатываем ветвь при условии: not state.get('error').
        if not state.get("error"):
            state["error"] = "BROWSER_POST_NOT_OBSERVED"
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            page.screenshot(path=str(path.with_suffix(".unknown.png")))
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            state["proof_capture_failed"] = True
        state["state"] = "unknown"
        save(path, state)
        return {"state": "unknown"}


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        settings = Settings.load(args.root)
        # Обрабатываем ветвь при условии: settings.app['max']['adapter_mode'] != 'browser'.
        if settings.app["max"]["adapter_mode"] != "browser":
            raise AgentError("BROWSER_MODE_NOT_ENABLED")
        payload = json.loads(sys.stdin.read(1_048_577))
        # Обрабатываем ветвь при условии: payload.get('config_hash') != settings.hash.
        if payload.get("config_hash") != settings.hash:
            raise AgentError("BROWSER_CONFIG_CHANGED")
        action = payload["action"]
        # Обрабатываем ветвь при условии: action == 'inspect'.
        if action == "inspect":
            result = inspect(settings, payload["chat_id"])
        # Обрабатываем ветвь при условии: action == 'publish'.
        elif action == "publish":
            authorize(settings, payload)
            result = publish(settings, payload, authorize_before_click=lambda: authorize(settings, payload))
        # Обрабатываем ветвь при условии: action == 'reconcile'.
        elif action == "reconcile":
            result = reconcile(settings, payload["chat_id"], payload["request_id"])
        # Обрабатываем ветвь при условии: action == 'correct-caption'.
        elif action == "correct-caption":
            from .browser_corrections import edit

            result = edit(settings, payload)
        else:
            # Альтернативная ветвь после проверки основного условия.
            raise AgentError("BROWSER_ACTION_INVALID")
        print(json.dumps(result, ensure_ascii=True))
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        print(json.dumps({"code": exc.code}))
    # Обрабатываем Exception и выбираем допустимый исход.
    except Exception:
        # Raw Playwright exceptions can contain page text or browser state.
        print(json.dumps({"code": "BROWSER_OPERATION_FAILED"}))


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    main()
