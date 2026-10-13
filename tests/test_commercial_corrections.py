from fastapi.testclient import TestClient
from sqlalchemy import select

from max_postiz_agent.api import create_app
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Draft, DraftRevision, Intent

from test_workflow import draft, ready_for_send


# Проверяем сценарий: commercial requires monetization.
def test_commercial_requires_monetization(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "owner-test-key")
    did = draft(service)
    headers = {"Authorization": "Bearer owner-test-key"}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service)) as c:
        response = c.post(
            f"/api/v1/drafts/{did}/commercial",
            json={"revision": 1, "commercial": True},
            headers=headers,
        )
        assert response.status_code == 200
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session() as s:
            h = fingerprint(s.get(Draft, did).current)
        response = c.post(
            f"/api/v1/drafts/{did}/approve",
            json={"revision": 1, "content_hash": h},
            headers=headers,
        )
        assert response.status_code == 422 and response.json()["code"] == "MONETIZATION_DISABLED"


# Проверяем сценарий: correction preserves revision history.
def test_correction_preserves_revision_history(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "owner-test-key")
    did = ready_for_send(service)
    assert service.publish_due() == 1
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        intent = session.scalar(select(Intent))
        iid = intent.id
        body = session.get(Draft, did).current["body"]
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service)) as c:
        response = c.post(
            f"/api/v1/publications/{iid}/correct",
            json={"revision": 1, "body": body},
            headers={"Authorization": "Bearer owner-test-key"},
        )
        assert response.status_code == 200
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        assert session.get(Draft, did).revision == 2
        assert len(session.scalars(select(DraftRevision)).all()) == 2
