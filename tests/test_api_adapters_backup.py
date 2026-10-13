import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from max_postiz_agent.adapters import MaxAPI, OpenCode, extract_object
from max_postiz_agent.api import create_app
from max_postiz_agent.backup import backup, restore
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Channel, Database, Inbox
from max_postiz_agent.service import Service


# Операция client: данного модуля.
@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "owner-test-key")
    monkeypatch.setenv("APP_EDITOR_SECRET", "editor-test-key")
    monkeypatch.setenv("APP_VIEWER_SECRET", "viewer-test-key")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "max-hook-test")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-hook-test")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service)) as client:
        yield client


# Операция headers: данного модуля.
def headers(role="owner"):
    return {"Authorization": "Bearer " + role + "-test-key"}


# Проверяем сценарий: api health and dashboard.
def test_api_health_and_dashboard(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert "Редакция" in client.get("/").text
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/static/unknown").status_code == 404


# Проверяем сценарий: api requires auth.
@pytest.mark.parametrize("path", ["channels", "drafts", "jobs", "audit", "integrations/status"])
def test_api_requires_auth(client, path):
    assert client.get("/api/v1/" + path).status_code == 401


# Проверяем сценарий: roles enforced.
def test_roles_enforced(client):
    assert client.get("/api/v1/channels", headers=headers("viewer")).status_code == 200
    assert client.post("/api/v1/channels/main/resume", headers=headers("viewer")).status_code == 403
    assert client.post("/api/v1/config/activate", headers=headers("editor")).status_code == 403
    assert (
        client.post(
            "/api/v1/channels/main/connect", json={"chat_id": "-123"}, headers=headers()
        ).status_code
        == 200
    )


# Проверяем сценарий: webhook secret and duplicate.
def test_webhook_secret_and_duplicate(client, service):
    event = {
        "update_id": 7,
        "channel_post": {"chat": {"id": -123}, "message_id": 1, "text": "Видео"},
    }
    assert client.post("/webhooks/telegram", json=event).status_code == 403
    hook = {"X-Telegram-Bot-Api-Secret-Token": "telegram-hook-test"}
    assert client.post("/webhooks/telegram", json=event, headers=hook).json()["accepted"] is True
    assert client.post("/webhooks/telegram", json=event, headers=hook).json()["accepted"] is False
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        assert len(session.scalars(select(Inbox)).all()) == 1


# Проверяем сценарий: event and request size.
def test_event_and_request_size(client):
    assert client.post("/webhooks/max", json={"text": "x" * 1100000}).status_code == 413
    assert (
        client.post(
            "/webhooks/max", json=[], headers={"X-Max-Bot-Api-Secret": "max-hook-test"}
        ).status_code
        == 422
    )


# Проверяем сценарий: metric origin and report.
def test_metric_origin_and_report(client):
    assert (
        client.post(
            "/api/v1/metrics",
            json={"channel_id": "main", "name": "subscribers", "value": 5},
            headers=headers(),
        ).status_code
        == 200
    )
    report = client.get("/api/v1/reports/main", headers=headers()).json()
    assert report["metrics"][0]["origin"] == "manual"


# Проверяем сценарий: editor invalid objects.
@pytest.mark.parametrize("text", ["[]", "not json", '{"x":1,"x":2}'])
def test_editor_invalid_objects(text):
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError):
        extract_object(text)


# Проверяем сценарий: opencode v2 shape and auth.
def test_opencode_v2_shape_and_auth(service, monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4107")
    monkeypatch.setenv("OPENCODE_MODEL_ID", "proxmox/qwen35-4b")
    monkeypatch.setenv("OPENCODE_AUTH_SECRET", "test-runtime-secret")
    service.settings.app["opencode"].update(adapter_mode="live", generate_transport="stateless")

    # Операция handle: данного модуля.
    def handle(request):
        assert request.headers["authorization"].startswith("Basic ")
        body = json.loads(request.content)
        assert body["model"] == {"providerID": "proxmox", "id": "qwen35-4b"}
        return httpx.Response(200, json={"data": {"text": "Нейровидео готово"}})

    editor = OpenCode(service.settings, transport=httpx.MockTransport(handle))
    assert editor.generate_text("prompt")[0] == "Нейровидео готово"
    editor.client.close()


# Проверяем сценарий: max delivery classification.
@pytest.mark.parametrize(
    "status,state",
    [
        (200, "confirmed"),
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (429, "retry_wait"),
        (500, "unknown"),
    ],
)
def test_max_delivery_classification(service, monkeypatch, status, state):
    service.settings.app["max"]["adapter_mode"] = "live"
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    calls = []

    # Операция handle: данного модуля.
    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "test-token"
        assert "test-token" not in str(request.url)
        return httpx.Response(
            status, json={"message": {"body": {"mid": "message-1"}}}, headers={"Retry-After": "12"}
        )

    publisher = MaxAPI(service.settings, transport=httpx.MockTransport(handle))
    delivery = publisher.publish("-123", "Текст")
    assert delivery.state == state and len(calls) == 1
    # Обрабатываем ветвь при условии: status == 429.
    if status == 429:
        assert delivery.retry_after == 12
    publisher.close()


# Проверяем сценарий: max timeout is unknown.
def test_max_timeout_is_unknown(service, monkeypatch):
    service.settings.app["max"]["adapter_mode"] = "live"
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")

    # Операция handle: данного модуля.
    def handle(request):
        raise httpx.ReadTimeout("unsafe detail", request=request)

    publisher = MaxAPI(service.settings, transport=httpx.MockTransport(handle))
    assert publisher.publish("-123", "Текст").state == "unknown"
    publisher.close()


# Проверяем сценарий: encrypted backup and restore.
def test_encrypted_backup_and_restore(service, project, tmp_path):
    key = Fernet.generate_key()
    path = tmp_path / "state.enc"
    digest = backup(service, path, key)
    assert len(digest) == 64 and "Нейро" not in path.read_bytes().decode("ascii")
    settings = service.settings
    db = Database(settings, url="sqlite:///" + (tmp_path / "restored.db").as_posix())
    db.initialize()
    target = Service(settings, db)
    manifest = restore(target, path, key)
    assert manifest["schema_version"] == "1.0"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with db.session() as session:
        assert session.get(Channel, "main").paused
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="EMPTY_DATABASE"):
        restore(target, path, key)
    target.close()


# Проверяем сценарий: wrong backup key rejected.
def test_wrong_backup_key_rejected(service, tmp_path):
    path = tmp_path / "state.enc"
    backup(service, path, Fernet.generate_key())
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="BACKUP_INVALID"):
        restore(service, path, Fernet.generate_key())
