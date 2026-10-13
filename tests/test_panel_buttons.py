"""Проверка всех кнопок в настоящем Chrome с изолированной базой и тестовыми адаптерами."""

from urllib.parse import urlsplit
import json

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from max_postiz_agent import image_pipeline, image_schedule, holidays, browser_corrections
from max_postiz_agent import panel_files
from max_postiz_agent.api import create_app
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Draft, Intent, MaterialMedia, VideoAsset, VideoProject


# Сеть браузера замыкаем на локальное приложение; ни один клик не обращается к MAX.
@pytest.fixture
def panel_browser(service, project, monkeypatch, clip):
    monkeypatch.setenv("APP_AUTH_SECRET", "button-owner-test-key")
    monkeypatch.setenv("APP_EDITOR_SECRET", "button-editor-test-key")
    monkeypatch.setenv("APP_VIEWER_SECRET", "button-viewer-test-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "button-pixabay-test-key")
    monkeypatch.setattr(service.publisher, "mode", "browser")
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {"publish": True, "binding_hash": fingerprint(service.settings.app["max"])})
    monkeypatch.setattr(service.publisher, "login", lambda chat: {"state": "login_window_requested", "pid": 1}, raising=False)
    service.connect("test", "-123")
    plan = {"title": "Тестовый ролик", "caption": "Тестовая визуальная история для канала.", "rights_basis": "Собственный материал",
            "scenes": [{"prompt": "A blue lake", "negative_prompt": "artifacts", "duration_seconds": 1}]}
    monkeypatch.setattr(service.hybrid, "generate_plan", lambda *a: plan)
    monkeypatch.setattr(service.hybrid, "approve_generation", lambda *a: None)
    monkeypatch.setattr(service.hybrid, "poll_project", lambda *a: None)
    monkeypatch.setattr(service.hybrid, "assemble", lambda *a: {"state": "assembled"})
    monkeypatch.setattr(service.hybrid, "cancel", lambda *a: None)
    monkeypatch.setattr(service.hybrid, "import_scene", lambda *a: {"state": "imported"})
    monkeypatch.setattr(service.hybrid, "curate", lambda *a: {"state": "curated"})
    monkeypatch.setattr(service, "approve", lambda *a: None)
    monkeypatch.setattr(service, "revise", lambda *a: None)
    monkeypatch.setattr(service, "reconcile", lambda *a: "confirmed")
    monkeypatch.setattr(browser_corrections, "_correct_caption", lambda *a: {"state": "confirmed"})
    monkeypatch.setattr(image_pipeline, "scan", lambda *a, **k: {"state": "ready", "top": []})
    # Настоящее расписание испытывают отдельные тесты; кнопка не запускает публикацию.
    monkeypatch.setattr(image_schedule, "tick", lambda *a, **k: {"state": "not_due", "next_run_at": "2026-10-11T20:00:00+03:00"})
    monkeypatch.setattr(holidays, "import_catalog", lambda *a: {"state": "updated", "holiday_count": 293})
    with service.db.session.begin() as db:
        db.add(VideoProject(id="video-buttons", channel_id="test", plan=plan, config_id=service.settings.hash))
        db.add(VideoAsset(id="asset-buttons", path=clip.relative_to(project).as_posix(),
                          sha256="a" * 64, frames=[], quality={"duration_seconds": 1}, provenance={}))
        db.add(MaterialMedia(id="media-buttons", material_id="material", url="https://example.org/video.mp4", mime="video/mp4"))
        for name, state in (("review", "review_pending"), ("published", "published"), ("unknown", "published")):
            db.add(Draft(id="draft-" + name, channel_id="test", story_id="story-" + name, state=state,
                         current={"title": "Тест", "body": "Тестовая подпись.", "checks": []}, config_id=service.settings.hash))
        db.add(Intent(id="intent-published", draft_id="draft-published", channel_id="test", state="confirmed", body_hash="h", published_at=1780000000))
    seen, errors = [], []
    with TestClient(create_app(service)) as client, sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        def handle(route):
            request = route.request
            path = urlsplit(request.url).path
            query = urlsplit(request.url).query
            headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length"}}
            seen.append((request.method, path, request.post_data_json if request.post_data else None))
            response = client.request(request.method, path + ("?" + query if query else ""), headers=headers,
                                      content=request.post_data_buffer)
            route.fulfill(status=response.status_code, body=response.content,
                          headers={key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "content-encoding"}})
        page.route("http://testserver/**", handle)
        page.goto("http://testserver/")
        yield page, service, seen, errors
        browser.close()


# Ждём результат действия, а не фиксированную задержку или случайную паузу.
def click(page, selector):
    button = page.locator(selector)
    button.click()
    expect(button).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("#feedback")).not_to_have_attribute("data-state", "working")


def login(page, role="owner"):
    page.locator("#token").fill("button-" + role + "-test-key")
    click(page, "#login")
    expect(page.locator("#status")).to_contain_text("Подключено")


# Читаемая история отличает посты от неудачных попыток, переводит даты и сохраняет пояснения к коду.
def test_publications_have_russian_labels_and_readonly_comments(panel_browser):
    page, service, seen, errors = panel_browser
    with service.db.session.begin() as db:
        db.add(Draft(id="draft-readable", channel_id="test", story_id="story-readable", state="published",
                     current={"title": "Проверка понятной карточки", "body": "Текст <script>не исполняется</script>"}, config_id=service.settings.hash))
        db.add(Intent(id="intent-readable", draft_id="draft-readable", channel_id="test", state="confirmed",
                      body_hash="h", send_started_at=1791738042, published_at=1791738051))
        # Отсутствующий черновик не должен превращать отклонённую отправку в опубликованный пост.
        db.add(Intent(id="intent-rejected", draft_id="missing-draft", channel_id="test", state="rejected",
                      body_hash="h", send_started_at=1791738000))
        db.add(Intent(id="intent-unknown-readable", draft_id="draft-unknown", channel_id="test", state="unknown",
                      body_hash="h", send_started_at=1791737990))
    login(page)
    click(page, "[data-tab='publications']")
    expect(page.locator("#publications-help")).to_be_visible()
    expect(page.locator("#publications-count")).to_contain_text("Опубликовано: 2 · Отклонено: 1")
    card = page.locator(".publication-card").first
    expect(card.get_by_role("heading")).to_have_text("Проверка понятной карточки")
    expect(card.locator(".publication-state")).to_have_text("Опубликовано")
    expect(card.locator(".publication-facts")).to_contain_text("Нейровидео — тест MAX")
    expect(card.locator(".publication-facts")).to_contain_text("11.10.2026, 20:00:51 · Europe/Moscow")
    expect(card.get_by_role("link", name="Открыть канал в MAX")).to_have_attribute("href", "https://web.max.ru/-123")
    expect(card.locator("pre")).not_to_be_visible()
    card.locator("summary").filter(has_text="Текущий текст поста").click()
    expect(card.locator(".publication-body")).to_have_text("Текст <script>не исполняется</script>")
    expect(card.locator("script")).to_have_count(0)
    card.locator("summary").filter(has_text="Техническая запись отправки").click()
    expect(card.locator("pre")).to_be_visible()
    expect(card.locator("pre")).to_contain_text("// Номер попытки отправки (intent_id)")
    expect(card.locator("pre")).to_contain_text("// Когда началась отправка")
    rejected = page.locator(".publication-card[data-state='rejected']")
    expect(rejected.locator(".publication-state")).to_have_text("Отправка отклонена")
    expect(rejected.locator(".publication-facts")).to_contain_text("Черновик недоступен")
    expect(rejected.get_by_role("button", name="Сверить результат")).to_have_count(0)
    expect(page.locator(".publication-card[data-state='unknown']").get_by_role("button", name="Сверить результат")).to_be_visible()
    # Чтение карточек, комментариев и подписи не вызывает ни одной отправки или правки.
    assert not [item for item in seen if item[0] == "POST" and item[1] != "/api/v1/session/login"]
    click(page, "[data-tab='sources']")
    expect(page.locator("#publications-help")).not_to_be_visible()
    expect(page.locator("#content summary").first).to_have_text("Состояние источника (JSON)")
    assert not errors


# Выбор файла, отмена и список проекта подставляют путь; workflow сохраняется вместе с выбранным графом.
def test_file_controls_select_cancel_browse_and_update_workflow(panel_browser, monkeypatch):
    page, service, seen, errors = panel_browser
    root = service.settings.root
    workflow = root / "config/новый workflow.json"
    workflow.write_text('{"1":{"class_type":"Example","inputs":{}}}', encoding="utf-8")
    monkeypatch.setattr(panel_files, "local_client", lambda request: True)
    monkeypatch.setattr(panel_files, "native_pick", lambda *args: {"state": "selected", "path": str(root / "fixture.mp4")})
    monkeypatch.setattr(panel_files, "explorer", lambda *args: {"state": "explorer_opened", "path": "fixture.mp4"})
    login(page)
    click(page, "[data-tab='drafts']")
    control = page.locator(".file-controls[data-path-key='Путь MP4']")
    control.get_by_role("button", name="Выбрать файл…", exact=True).click()
    expect(page.get_by_label("Путь MP4", exact=True)).to_have_value("fixture.mp4")
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    monkeypatch.setattr(panel_files, "native_pick", lambda *args: {"state": "cancelled"})
    control.get_by_role("button", name="Выбрать файл…", exact=True).click()
    expect(page.locator("#feedback")).to_contain_text("Выбор отменён")
    expect(page.get_by_label("Путь MP4", exact=True)).to_have_value("fixture.mp4")
    control.get_by_role("button", name="В Проводнике", exact=True).click()
    expect(page.locator("#feedback")).to_contain_text("Папка открыта в Проводнике")
    # Встроенный список не требует диалога Windows и работает на удалённой виртуальной машине.
    control.get_by_role("button", name="Файлы проекта", exact=True).click()
    expect(page.locator("#file-picker")).to_be_visible()
    page.locator("#file-picker").get_by_role("button", name="Папка: config", exact=True).click()
    expect(page.locator("#file-picker-path")).to_contain_text("config")
    click(page, "#file-picker-up")
    page.locator("#file-picker").get_by_role("button", name="Файл: fixture.mp4", exact=True).click()
    expect(page.locator("#file-picker")).not_to_be_visible()
    expect(page.get_by_label("Путь MP4", exact=True)).to_have_value("fixture.mp4")
    # Если рабочий стол недоступен, обычная кнопка выбора открывает встроенный список.
    monkeypatch.setattr(panel_files, "local_client", lambda request: False)
    control.get_by_role("button", name="Выбрать файл…", exact=True).click()
    expect(page.locator("#file-picker")).to_be_visible()
    click(page, "#file-picker-close")
    expect(page.get_by_label("Путь MP4", exact=True)).to_have_value("fixture.mp4")
    monkeypatch.setattr(panel_files, "local_client", lambda request: True)
    click(page, "#settings-tab")
    monkeypatch.setattr(panel_files, "native_pick", lambda *args: {"state": "selected", "path": str(workflow)})
    control = page.locator(".file-controls[data-path-key='video.workflow_path']")
    control.get_by_role("button", name="Выбрать файл…", exact=True).click()
    expect(page.locator("input[data-path-key='video.workflow_path']")).to_have_value("config/новый workflow.json")
    click(page, "#settings-validate")
    sent = [body for method, path, body in seen if path == "/api/v1/settings/validate"][-1]
    assert sent["workflow"]["1"]["class_type"] == "Example"
    assert sent["app"]["video"]["workflow_path"] == "config/новый workflow.json"
    # Выбор папки изменяет только поле JSON; применение конфигурации выполняется отдельно.
    control = page.locator(".file-controls[data-path-key='media_dir']")
    control.get_by_role("button", name="Файлы проекта", exact=True).click()
    click(page, "#file-picker-root")
    click(page, "#file-picker-folder")
    expect(page.locator("#file-picker")).not_to_be_visible()
    assert json.loads(page.locator("#settings-json").input_value())["media_dir"] == "."
    assert not [item for item in seen if item[1].endswith("/settings/save") or item[1].endswith("/video")]
    assert not errors


# Все статические кнопки дают результат; повторное подключение и reload сохраняют вход.
def test_all_static_panel_buttons(panel_browser):
    page, service, seen, errors = panel_browser
    expect(page.locator("#browser-login")).to_be_disabled()
    login(page)
    expect(page.locator("#channel")).to_have_value("test")
    expect(page.locator("#chat")).to_have_value("-123")
    click(page, "#login")
    page.reload()
    expect(page.locator("#status")).to_contain_text("Сессия восстановлена")
    for button in page.locator("[data-tab]").all():
        button.click()
        expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    for button_id in ("refresh", "browser-login", "browser-check", "connect", "pause", "resume", "report", "bootstrap", "growth", "images-status", "images-scan", "images-pause", "images-resume", "images-tick"):
        # Рабочие кнопки расположены в основном разделе.
        click(page, "[data-tab='channels']")
        click(page, "#" + button_id)
        expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    page.locator("#video-idea").fill("Тестовый ролик")
    click(page, "#generate-plan")
    click(page, "#create-plan")
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    click(page, "#calendar-tab")
    expect(page.locator(".calendar-day")).to_have_count(7)
    # На исходе воскресенья текущая неделя может не содержать будущих событий.
    click(page, "#calendar-next")
    page.locator(".calendar-event").first.click()
    expect(page.locator("#calendar-detail")).to_be_visible()
    for button_id in ("calendar-prev", "calendar-next", "calendar-today", "calendar-refresh", "holidays-toggle", "holidays-update"):
        click(page, "#" + button_id)
        expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    page.locator("#schedule-enabled").uncheck()
    click(page, "#schedule-save")
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    click(page, "#settings-tab")
    for button_id in ("channel-profiles", "channel-apply", "settings-load", "settings-format", "settings-validate", "settings-save", "validate", "activate"):
        click(page, "#" + button_id)
        expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    page.locator("[data-credential='PIXABAY_API_KEY']").fill("new-button-key")
    click(page, "#credentials-save")
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    click(page, "#logout")
    expect(page.locator("#browser-login")).to_be_disabled()
    assert not errors
    assert any(path == "/api/v1/browser/check" for _, path, _ in seen)


# Ошибки полей, неверный ключ, повреждённый JSON и недостаточные права видны рядом с действиями.
def test_panel_validation_and_viewer_controls(panel_browser):
    page, service, seen, errors = panel_browser
    page.locator("#token").fill("wrong-key")
    click(page, "#login")
    expect(page.locator("#feedback")).to_have_attribute("data-state", "error")
    login(page)
    page.locator("#chat").fill("")
    click(page, "#browser-check")
    expect(page.locator("#feedback")).to_contain_text("Укажите chat_id")
    page.locator("#video-plan").fill("{broken")
    click(page, "#create-plan")
    expect(page.locator("#feedback")).to_contain_text("JSON")
    click(page, "#logout")
    login(page, "viewer")
    expect(page.locator("#settings-tab")).to_be_disabled()
    expect(page.locator("#browser-check")).to_be_disabled()
    click(page, "#calendar-tab")
    expect(page.locator("#schedule-save")).to_be_disabled()
    assert not errors


# Кнопки записей отправляют правильные контракты; предпросмотр не исчезает после ответа.
def test_all_dynamic_record_buttons(panel_browser, monkeypatch):
    page, service, seen, errors = panel_browser
    login(page)
    click(page, "[data-tab='sources']")
    click(page, "#content article:first-child button")
    click(page, "[data-tab='videos']")
    for label in ("Одобрить генерацию", "Проверить результат", "Собрать ролик", "Остановить проект"):
        page.get_by_role("button", name=label, exact=True).click()
        expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    page.get_by_label("Путь готовой сцены").fill("fixture.mp4")
    page.get_by_label("Права на сцену").fill("Собственные права")
    page.get_by_role("button", name="Импортировать готовую сцену", exact=True).click()
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    click(page, "[data-tab='source-media']")
    page.get_by_label("Подпись подборки").fill("Проверенная подпись")
    page.get_by_role("button", name="Подготовить подборку", exact=True).click()
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    click(page, "[data-tab='assets']")
    page.get_by_role("button", name="Просмотреть ролик", exact=True).click()
    expect(page.locator("#content video")).to_have_count(1)
    click(page, "[data-tab='drafts']")
    page.get_by_label("Путь MP4").fill("fixture.mp4")
    page.get_by_label("Основание права на публикацию").fill("Собственные права")
    for label in ("Приложить MP4", "Коммерческий материал", "Снять коммерческую отметку", "Сохранить новую ревизию", "Одобрить в календарь"):
        page.get_by_role("button", name=label, exact=True).click()
        expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    page.get_by_role("button", name="Исправить опубликованную подпись", exact=True).first.click()
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    page.get_by_role("button", name="Отклонить", exact=True).click()
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    with service.db.session.begin() as db:
        db.add(Intent(id="intent-unknown", draft_id="draft-unknown", channel_id="test", state="unknown", body_hash="h"))
    click(page, "[data-tab='publications']")
    page.get_by_role("button", name="Сверить результат", exact=True).click()
    expect(page.locator("#feedback")).to_have_attribute("data-state", "success")
    assert not errors
