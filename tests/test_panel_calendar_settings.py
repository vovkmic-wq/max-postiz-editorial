"""Проверяем сессии панели, календарь, настройки и праздничный поиск без реальной отправки."""

import copy
import shutil
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from max_postiz_agent import image_pipeline as images
from max_postiz_agent.api import create_app
from max_postiz_agent.channel_profiles import atomic_json
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Draft, Intent
from max_postiz_agent.holidays import campaign, catalog, for_day, easter
from max_postiz_agent.image_schedule import MOSCOW, status, tick
from max_postiz_agent.panel_auth import decode_session, encode_session
from max_postiz_agent.panel_calendar import calendar, save_schedule
from max_postiz_agent.panel_settings import snapshot


# Каждый тест использует собственную базу и фиктивные ключи доступа.
@pytest.fixture
def panel_client(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "panel-owner-test-key")
    monkeypatch.setenv("APP_VIEWER_SECRET", "panel-viewer-test-key")
    monkeypatch.setenv("APP_EDITOR_SECRET", "panel-editor-test-key")
    with TestClient(create_app(service)) as client:
        yield client


def owner():
    return {"Authorization": "Bearer panel-owner-test-key"}


# Обновление страницы продолжает сессию; повторный вход без ключа её не разрушает.
def test_session_survives_reload_and_explicit_logout(panel_client):
    assert panel_client.get("/api/v1/session").status_code == 401
    response = panel_client.post("/api/v1/session/login", headers=owner(), json={})
    assert response.json()["role"] == "owner"
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    assert "panel-owner-test-key" not in response.text
    assert panel_client.get("/api/v1/channels").status_code == 200
    headers = {"Origin": "http://testserver"}
    assert panel_client.post("/api/v1/session/login", headers=headers, json={}).status_code == 200
    assert panel_client.post("/api/v1/session/logout", headers=headers, json={}).status_code == 200
    assert panel_client.get("/api/v1/channels").status_code == 401


# Сторонний сайт не может использовать сессию для изменения настройки канала.
def test_cookie_csrf_and_viewer_role_are_enforced(panel_client):
    panel_client.post("/api/v1/session/login", headers=owner(), json={})
    assert panel_client.post("/api/v1/channels/test/pause", json={}).status_code == 403
    assert panel_client.post("/api/v1/channels/test/pause", headers={"Origin": "http://evil.example"}, json={}).status_code == 403
    assert panel_client.post("/api/v1/channels/test/pause", headers={"Origin": "http://testserver"}, json={}).status_code == 200
    panel_client.cookies.clear()
    panel_client.post("/api/v1/session/login", headers={"Authorization": "Bearer panel-viewer-test-key"}, json={})
    assert panel_client.get("/api/v1/calendar?channel_id=test").status_code == 200
    assert panel_client.get("/api/v1/settings").status_code == 403


# Подмена роли, истечение срока и замена ключа отзывают сессию.
def test_session_signature_expiry_and_key_rotation(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "panel-owner-test-key")
    token = encode_session(service.settings, "owner", at=100)
    assert decode_session(service.settings, token, at=101) == "owner"
    for bad in (token + "x", "not-a-session", "a.b", ""):
        with pytest.raises(AgentError):
            decode_session(service.settings, bad, at=101)
    with pytest.raises(AgentError, match="SESSION_EXPIRED"):
        decode_session(service.settings, token, at=100 + 8 * 3600)
    monkeypatch.setenv("APP_AUTH_SECRET", "rotated-panel-key")
    with pytest.raises(AgentError):
        decode_session(service.settings, token, at=101)


# Календарь показывает реальный пост на нужной дате и точное время будущего запуска.
def test_calendar_week_dates_and_actual_publication(service, project):
    instant = datetime(2026, 10, 11, 12, tzinfo=MOSCOW)
    with service.db.session.begin() as db:
        db.add(Draft(id="calendar-draft", channel_id="test", story_id="calendar-story", state="published",
                     current={"title": "Проверенный пост", "body": "Текст"}, config_id=service.settings.hash))
        db.add(Intent(draft_id="calendar-draft", channel_id="test", body_hash="hash", state="confirmed", published_at=instant.timestamp()))
    result = calendar(service, "test", "2026-10-11", at=instant)
    assert result["week_start"] == "2026-10-05"
    assert result["days"][0]["weekday"] == "Понедельник"
    assert result["days"][6]["events"][0]["title"] == "Проверенный пост"
    assert result["days"][6]["events"][0]["time"] == "12:00"
    with pytest.raises(AgentError, match="CALENDAR_DATE_INVALID"):
        calendar(service, "test", "wrong-date")


# Недельное расписание учитывает минуты и выбранные дни; поздний старт не догоняет отправку.
def test_weekly_schedule_matches_calendar_and_does_not_catch_up(service, project, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "panel-owner-test-key")
    values = {"weekdays": [0, 2, 4], "times": ["09:15", "18:30"], "timezone": "Europe/Moscow",
              "enabled": False, "min_gap_minutes": 120, "max_posts_per_day": 4}
    save_schedule(service, "test", values)
    from max_postiz_agent.image_schedule import configure
    sunday = datetime(2026, 10, 11, 17, tzinfo=MOSCOW)
    configured = configure(project, True, at=sunday)
    assert configured["next_run_at"] == "2026-10-12T09:15:00+03:00"
    monday = sunday + timedelta(days=1)
    calls = []
    def runner(root, at):
        calls.append(at)
        return {"state": "confirmed"}
    assert tick(project, at=monday.replace(hour=9, minute=15), runner=runner)["state"] == "confirmed"
    assert tick(project, at=monday.replace(hour=9, minute=16), runner=runner)["state"] == "already_checked"
    assert tick(project, at=monday.replace(hour=19), runner=runner)["state"] == "not_due"
    assert len(calls) == 1
    shown = calendar(service, "test", at=sunday)
    assert shown["schedule"]["times"] == ["09:15", "18:30"]


# Неверные часы и ограничения не изменяют действующий профиль или файлы.
def test_invalid_settings_are_atomic_and_keys_are_write_only(service, project, panel_client, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "previous-private-marker")
    response = panel_client.get("/api/v1/settings", headers=owner())
    value = response.json()["configuration"]
    assert "previous-private-marker" not in response.text
    before = (project / "TOPIC.md").read_bytes()
    invalid = copy.deepcopy(value)
    invalid["topic"]["channels"][0]["publication"]["slots"] = ["25:00"]
    assert panel_client.post("/api/v1/settings/save", headers=owner(), json=invalid).status_code == 422
    assert (project / "TOPIC.md").read_bytes() == before
    new = "new-private-test-value"
    result = panel_client.post("/api/v1/settings/credentials", headers=owner(), json={"PIXABAY_API_KEY": new})
    assert result.status_code == 200 and new not in result.text
    assert new in (project / ".env.local").read_text(encoding="utf-8")
    assert new not in panel_client.get("/api/v1/settings", headers=owner()).text
    assert panel_client.post("/api/v1/settings/credentials", headers=owner(), json={"PATH": "changed"}).status_code == 422


# Полная конфигурация сохраняется через API и остаётся доступной после повторной загрузки.
def test_save_all_settings_and_weekly_limits(service, project, panel_client):
    value = snapshot(service)["configuration"]
    value["image_sources"]["min_likes"] = 125
    assert panel_client.post("/api/v1/settings/validate", headers=owner(), json=value).json()["state"] == "valid"
    result = panel_client.post("/api/v1/settings/save", headers=owner(), json=value)
    assert result.status_code == 200 and result.json()["schedules_paused"]
    assert snapshot(service)["configuration"]["image_sources"]["min_likes"] == 125
    assert status(project)["state"] == "paused"


# Изменение настроек пересоздаёт редактор; URL/ключ перечитываются без ошибки закрытия клиента.
def test_editor_settings_and_credentials_rebuild_adapter(service, panel_client, monkeypatch):
    old = service.editor
    value = snapshot(service)["configuration"]
    value["app"]["opencode"]["model_env"] = "OPENCODE_MODEL_ID"
    value["app"]["opencode"]["required_version"] = "panel-test-version"
    assert panel_client.post("/api/v1/settings/save", headers=owner(), json=value).status_code == 200
    assert service.editor is not old
    old = service.editor
    monkeypatch.setenv("OPENCODE_AUTH_SECRET", "old-fake-secret")
    response = panel_client.post("/api/v1/settings/credentials", headers=owner(), json={"OPENCODE_AUTH_SECRET": "new-fake-secret"})
    assert response.status_code == 200 and service.editor is not old


# Отдельная суточная отметка и старые журналы защищают от повторного праздничного поиска.
def test_holiday_first_run_only_and_existing_daily_history(project):
    value = {"version": 1, "enabled": True, "calendar_year": 2026,
             "holidays": [{"id": "newyear", "name": "Новый год", "date": "2026-01-01", "source_url": "https://example.org/holiday", "tags": ["new year"]}]}
    atomic_json(project, "config/holidays.json", value)
    first = datetime(2026, 1, 1, tzinfo=MOSCOW)
    preview = campaign(project, "test", first)
    assert preview["holiday_names"] == ["Новый год"]
    live = campaign(project, "test", first, reserve=True, slot="2026-01-01-00")
    assert live["first_run_of_day"]
    assert not campaign(project, "test", first + timedelta(hours=4))["holiday_names"]
    # Другой канал получает собственную первую попытку того же дня.
    assert campaign(project, "nature", first)["holiday_names"] == ["Новый год"]
    atomic_json(project, "data/image-ranking/channels/nature/auto-runs/2026-01-01-00.json", {"state": "confirmed"})
    assert not campaign(project, "nature", first + timedelta(hours=4))["holiday_names"]


# Подтверждённые правила не закрепляют переходящий праздник за прошлогодним числом.
def test_holiday_catalog_dates_and_recurring_rules(project):
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    shutil.copy2(root / "config/holidays.example.json", project / "config/holidays.json")
    assert len(catalog(project)["holidays"]) > 650
    assert easter(2026).isoformat() == "2026-04-12"
    assert easter(2027).isoformat() == "2027-05-02"
    from datetime import date
    assert any("сельского" in row["name"] for row in for_day(project, date(2026, 10, 11)))
    assert any("Новый год" in row["name"] for row in for_day(project, date(2027, 1, 1)))


# Праздничный запрос выполняется один раз; следующий слот сканирует обычные источники.
def test_auto_run_holiday_search_then_normal(service, project, monkeypatch):
    from test_browser_publishing import browser_config
    service.activate(browser_config(project))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {"publish": True, "binding_hash": fingerprint(service.settings.app["max"])})
    service.connect("test", "-123")
    atomic_json(project, "config/holidays.json", {"version": 1, "enabled": True, "calendar_year": 2026,
                "holidays": [{"id": "holiday", "name": "Новый год", "date": "2026-01-01", "source_url": "https://example.org/newyear", "tags": ["new year"]}]})
    calls = []
    def scanner(root, **options):
        calls.append(options)
        return {"state": "ready", "top": [{"rank": 1, "source_id": "source", "id": str(len(calls)), "sha256": str(len(calls)) * 64, "page_url": "https://example.org/" + str(len(calls))}]}
    monkeypatch.setattr(images, "scan", scanner)
    def publisher(*args, **kwargs):
        return {"state": "confirmed"}
    at = datetime(2026, 1, 1, tzinfo=MOSCOW)
    first = images.auto_run(service, at=at, publisher=publisher)
    second = images.auto_run(service, at=at + timedelta(hours=4), publisher=publisher)
    assert first["holiday_search"]["holiday_names"] == ["Новый год"]
    assert "Новый год" in calls[0]["cfg"]["sources"][0]["queries"]
    assert calls[0]["cfg"]["top_n"] == 1
    assert "cfg" not in calls[1] and second["state"] == "confirmed"
