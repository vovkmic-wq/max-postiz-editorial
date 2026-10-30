import json
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from max_postiz_agent import browser_worker
from max_postiz_agent.adapters import Delivery
from max_postiz_agent.browser_publisher import BrowserMax
from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.db import Intent

from test_workflow import ready_for_send
from conftest import write_topic


# Операция browser_config: данного модуля.
def browser_config(project):
    path = project / "config/app.json"
    app = json.loads(path.read_text())
    app["max"].update(adapter_mode="browser", browser={
        "profile_dir": "data/max-browser", "channel": "chrome", "headless": True,
        "operation_timeout_seconds": 30, "channels": {"-123": "Тест"},
    })
    path.write_text(json.dumps(app))
    return Settings.load(project)


# Проверяем сценарий: browser is only a max adapter.
def test_browser_is_only_a_max_adapter(project):
    settings = browser_config(project)
    assert settings.app["max"]["adapter_mode"] == "browser"
    settings.app["opencode"]["adapter_mode"] = "browser"
    (project / "config/app.json").write_text(json.dumps(settings.app))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="ADAPTER_MODE_INVALID"):
        Settings.load(project)


# Проверяем сценарий: browser profile never uses media or tracked directories.
@pytest.mark.parametrize("profile", ["data/../config", "data", "data/media", "../outside"])
def test_browser_profile_never_uses_media_or_tracked_directories(project, profile):
    settings = browser_config(project)
    settings.app["max"]["browser"]["profile_dir"] = profile
    (project / "config/app.json").write_text(json.dumps(settings.app))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError):
        Settings.load(project)


# Проверяем сценарий: channel rebinding changes approval hash.
def test_channel_rebinding_changes_approval_hash(project):
    settings = browser_config(project)
    before = settings.hash
    settings.app["max"]["browser"]["channels"]["-123"] = "Другой канал"
    assert settings.hash != before


# Проверяем сценарий: browser never enables autopublish.
def test_browser_never_enables_autopublish(project):
    settings = browser_config(project)
    settings.topic["channels"][0]["publication"]["mode"] = "autopublish"
    write_topic(project, settings.topic)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="BROWSER_REQUIRES_REVIEW"):
        Settings.load(project)


# Проверяем сценарий: browser controls require owner.
def test_browser_controls_require_owner(service, monkeypatch):
    from fastapi.testclient import TestClient
    from max_postiz_agent.api import create_app

    service.activate(browser_config(service.settings.root))
    monkeypatch.setenv("APP_AUTH_SECRET", "browser-owner-key")
    monkeypatch.setenv("APP_EDITOR_SECRET", "browser-editor-key")
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {"publish": True})
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service)) as api:
        assert api.post("/api/v1/browser/check", json={"chat_id": "-123"}, headers={"Authorization": "Bearer browser-editor-key"}).status_code == 403
        assert api.post("/api/v1/browser/check", json={"chat_id": "-123"}, headers={"Authorization": "Bearer browser-owner-key"}).status_code == 200


# Проверяем сценарий: profile lock is released after exception.
def test_profile_lock_is_released_after_exception(tmp_path):
    lock = tmp_path / "profile.lock"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(RuntimeError):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with profile_lock(lock):
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with pytest.raises(AgentError, match="PROFILE_BUSY"):
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with profile_lock(lock):
                    pytest.fail("concurrent profile opened")
            raise RuntimeError("process error")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with profile_lock(lock):
        pass


# Проверяем сценарий: saved sending intent never opens browser again.
def test_saved_sending_intent_never_opens_browser_again(project, monkeypatch):
    settings = browser_config(project)
    payload = {"action": "publish", "request_id": "test-intent", "chat_id": "-123",
               "text": "Тест", "attachment": None, "config_hash": settings.hash}
    path = browser_worker.ledger_path(settings, payload["request_id"])
    browser_worker.save(path, {"state": "sending", "content_hash": fingerprint(payload)})
    monkeypatch.setattr(browser_worker, "session", lambda *a: pytest.fail("browser reopened"))
    assert browser_worker.publish(settings, payload) == {"state": "unknown"}
    payload["text"] = "Другая публикация"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="INTENT_CONTENT_CHANGED"):
        browser_worker.publish(settings, payload)


# Проверяем сценарий: changed video is not uploaded.
def test_changed_video_is_not_uploaded(project):
    settings = browser_config(project)
    path = project / "data/media/video.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"changed")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="VIDEO_HASH_CHANGED"):
        browser_worker.snapshot_video(settings, {"type": "browser-video", "payload": {
            "path": "data/media/video.mp4", "sha256": "0" * 64,
        }}, "intent")


# Код дочернего процесса без устойчивого доказательства не разрешает замену или повтор.
def test_profile_or_login_failure_without_proof_stays_unknown(project, monkeypatch):
    publisher = BrowserMax(browser_config(project))
    # Операция failed: данного модуля.
    def failed(*args, **kwargs):
        raise AgentError("BROWSER_LOGIN_OR_CHANNEL_REQUIRED")
    monkeypatch.setattr(publisher, "call", failed)
    assert publisher.publish("-123", "Тест", request_id="intent").state == "unknown"
    # Операция unknown: данного модуля.
    def unknown(*args, **kwargs):
        raise AgentError("BROWSER_OPERATION_UNCONFIRMED")
    monkeypatch.setattr(publisher, "call", unknown)
    assert publisher.publish("-123", "Тест", request_id="intent").state == "unknown"


# Проверяем сценарий: service passes durable intent and never repeats unknown.
def test_service_passes_durable_intent_and_never_repeats_unknown(service, monkeypatch):
    service.activate(browser_config(service.settings.root))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    calls = []
    # Операция send: данного модуля.
    def send(chat, body, attachment=None, *, request_id=None):
        calls.append(request_id)
        return Delivery("unknown")
    monkeypatch.setattr(service.publisher, "publish", send)
    ready_for_send(service)
    assert service.publish_due() == 0
    assert service.publish_due() == 0
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as db:
        intent = db.scalar(select(Intent))
        assert calls == [intent.id] and intent.state == "unknown"
    monkeypatch.setattr(service.publisher, "reconcile", lambda chat, rid: Delivery("confirmed", "browser-" + rid))
    assert service.reconcile(calls[0]) == "confirmed"


# Проверяем сценарий: worker rechecks pause and exact approval.
def test_worker_rechecks_pause_and_exact_approval(service, monkeypatch):
    service.activate(browser_config(service.settings.root))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    captured = []
    # Операция queued: данного модуля.
    def queued(chat, body, attachment=None, *, request_id=None):
        captured.append({"request_id": request_id, "chat_id": chat, "text": body, "attachment": attachment})
        browser_worker.authorize(service.settings, captured[-1])
        return Delivery("unknown")
    monkeypatch.setattr(service.publisher, "publish", queued)
    ready_for_send(service)
    service.publish_due()
    # Emulate the worker still being at its pre-click stage.
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as db:
        db.get(Intent, captured[0]["request_id"]).state = "sending"
    changed = {**captured[0], "text": "Подменённый текст"}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="INTENT_NOT_APPROVED"):
        browser_worker.authorize(service.settings, changed)
    service.pause("main")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="INTENT_NOT_APPROVED"):
        browser_worker.authorize(service.settings, captured[0])


# Операция browser_page: данного модуля.
@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        page = browser.new_page()
        # An isolated HTML fixture; no request reaches the real MAX service.
        html = '''<meta charset="utf-8"><h2 id="main-header-title" style="width:0;height:0;overflow:hidden">Окно чата с Тест</h2>
        <div id="posts"></div><div contenteditable role="textbox" aria-placeholder="Пост" style="min-height:24px"></div>
        <button aria-label="Загрузить файл" onclick="document.querySelector('#menu').hidden=false">+</button>
        <div id="menu" hidden><button onclick="document.querySelector('input').click()">Фото или видео</button></div>
        <input type="file" hidden><button aria-label="Отправить сообщение" onclick="send()">Send</button>
        <script>
        function render(){document.querySelector('#posts').innerHTML='';
          for(const post of JSON.parse(localStorage.getItem('posts')||'[]')){
            const item=document.createElement('div');item.setAttribute('role','listitem');
            const text=document.createElement('span');text.textContent=post.text;item.append(text);
            if(post.kind){const media=document.createElement(post.kind);if(post.kind==='img')media.src='blob:test-image';item.append(media)}
            document.querySelector('#posts').append(item);}}
        function send(){const editor=document.querySelector('[contenteditable]');
          const posts=JSON.parse(localStorage.getItem('posts')||'[]');
          const file=document.querySelector('input').files[0];
          posts.push({text:editor.innerText,kind:file?(file.name.endsWith('.mp4')?'video':'img'):null});
          localStorage.setItem('posts',JSON.stringify(posts));editor.textContent='';render();}render();
        </script>'''
        page.route("https://web.max.ru/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        yield page
        browser.close()


# Проверяем сценарий: real browser text receipt survives reload.
def test_real_browser_text_receipt_survives_reload(project, browser_page, monkeypatch):
    settings = browser_config(project)
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*args):
        yield browser_page
    monkeypatch.setattr(browser_worker, "session", opened)
    payload = {"request_id": "publish-text", "chat_id": "-123", "text": "Браузерный тест", "attachment": None}
    assert browser_worker.publish(settings, payload)["state"] == "confirmed"
    assert browser_worker.publish(settings, payload)["state"] == "confirmed"
    assert browser_page.get_by_role("listitem").count() == 1


# Проверяем сценарий: browser waits for optimistic post to persist.
def test_browser_waits_for_optimistic_post_to_persist(project, browser_page, monkeypatch):
    settings = browser_config(project)
    html = '''<meta charset="utf-8"><h2 id="main-header-title">Окно чата с Тест</h2>
    <div id="posts"></div><div contenteditable role="textbox" aria-placeholder="Пост"></div>
    <button aria-label="Отправить сообщение" onclick="send()">Send</button>
    <script>
    function render(text){document.querySelector('#posts').innerHTML = text
      ? '<div role="listitem"><span>'+text+'</span></div>' : '';}
    render(localStorage.getItem('delayed-post'));
    function send(){const editor=document.querySelector('[contenteditable]');
      const text=editor.innerText;editor.textContent='';render(text);
      setTimeout(()=>localStorage.setItem('delayed-post', text),1500);}
    </script>'''
    browser_page.route("https://web.max.ru/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*args):
        yield browser_page
    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.publish(settings, {"request_id": "delayed-post", "chat_id": "-123",
                                              "text": "Сохранённый пост", "attachment": None})
    assert result["state"] == "confirmed"
    assert browser_page.get_by_role("listitem").count() == 1


# Проверяем сценарий: real browser video upload and receipt.
def test_real_browser_video_upload_and_receipt(project, browser_page, monkeypatch, clip):
    settings = browser_config(project)
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*args):
        yield browser_page
    monkeypatch.setattr(browser_worker, "session", opened)
    attachment = BrowserMax(settings).upload_video(project / "fixture.mp4")
    result = browser_worker.publish(settings, {
        "request_id": "publish-video", "chat_id": "-123", "text": "Ролик", "attachment": attachment,
    })
    assert result["state"] == "confirmed"
    assert browser_page.get_by_role("listitem").locator("video").count() == 1


# Проверяем сценарий: real browser image upload and receipt.
def test_real_browser_image_upload_and_receipt(project, browser_page, monkeypatch):
    from PIL import Image

    settings = browser_config(project)
    picture = project / "fixture.png"
    Image.new("RGB", (512, 512), "navy").save(picture)
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*args):
        yield browser_page
    monkeypatch.setattr(browser_worker, "session", opened)
    attachment = BrowserMax(settings).upload_image(picture)
    result = browser_worker.publish(settings, {
        "request_id": "publish-image", "chat_id": "-123", "text": "Изображение",
        "attachment": attachment,
    })
    assert result["state"] == "confirmed"
    assert browser_page.get_by_role("listitem").locator("img").count() == 1


# Проверяем сценарий: click succeeded but confirmation failed never sends twice.
def test_click_succeeded_but_confirmation_failed_never_sends_twice(project, browser_page, monkeypatch):
    settings = browser_config(project)
    # Операция opened: данного модуля.
    @contextmanager
    def opened(*args):
        yield browser_page
    monkeypatch.setattr(browser_worker, "session", opened)
    # Операция lost_reload: данного модуля.
    def lost_reload(*args, **kwargs):
        raise RuntimeError("confirmation connection lost")
    monkeypatch.setattr(browser_page, "reload", lost_reload)
    payload = {"request_id": "lost-after-click", "chat_id": "-123",
               "text": "Единственная публикация", "attachment": None}
    assert browser_worker.publish(settings, payload)["state"] == "unknown"
    assert browser_worker.publish(settings, payload)["state"] == "unknown"
    assert browser_page.get_by_role("listitem").count() == 1
    assert browser_worker.reconcile(settings, "-123", "lost-after-click")["state"] == "confirmed"
    assert browser_page.get_by_role("listitem").count() == 1
