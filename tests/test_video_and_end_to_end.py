import json

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.adapters import OpenCode
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.db import Draft, Intent, Material, Story, now
from max_postiz_agent.media import attach_video, inspect_video
from max_postiz_agent.network import SafeHTTP

from conftest import write_topic
from test_network_collectors import resolver
from test_workflow import draft


# Проверяем сценарий: short mp4 inspection.
def test_short_mp4_inspection(project, clip):
    info = inspect_video(project, "fixture.mp4")
    assert info["duration_seconds"] == 1 and info["bytes"] > 0 and len(info["sha256"]) == 64


# Проверяем сценарий: bad video and duration rejected.
def test_bad_video_and_duration_rejected(project, clip):
    (project / "corrupt.mp4").write_bytes(b"not a video")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="VIDEO_STREAM_INVALID"):
        inspect_video(project, "corrupt.mp4")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="VIDEO_DURATION_EXCEEDED"):
        inspect_video(project, "fixture.mp4", duration_limit=0.5)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="VIDEO_TOO_LARGE"):
        inspect_video(project, "fixture.mp4", max_bytes=10)


# Проверяем сценарий: video rights and revision.
def test_video_rights_and_revision(service, clip):
    did = draft(service)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="VIDEO_RIGHTS_REQUIRED"):
        attach_video(service, did, "fixture.mp4", "")
    info = attach_video(service, did, "fixture.mp4", "Собственный технический тестовый ролик")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        d = session.get(Draft, did)
        assert d.revision == 2 and d.current["video"]["sha256"] == info["sha256"]
        assert d.state == "review_pending"


# Проверяем сценарий: video hash dedup.
def test_video_hash_dedup(service, clip):
    did = draft(service)
    attach_video(service, did, "fixture.mp4", "Собственный технический тестовый ролик")
    attach_video(service, did, "fixture.mp4", "Собственный технический тестовый ролик")
    assert len(list((service.settings.root / "data/media").glob("*.mp4"))) == 1


# Проверяем сценарий: opencode session transport cleanup.
def test_opencode_session_transport_cleanup(service, monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4107")
    monkeypatch.setenv("OPENCODE_MODEL_ID", "proxmox/qwen35-4b")
    service.settings.app["opencode"].update(
        adapter_mode="live", generate_transport="session", runtime_dir="data/opencode-runtime"
    )
    calls = []

    # Операция handle: данного модуля.
    def handle(request):
        calls.append((request.method, request.url.path))
        # Обрабатываем ветвь при условии: request.url.path == '/api/session'.
        if request.url.path == "/api/session":
            value = json.loads(request.content)
            assert value["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
            return httpx.Response(200, json={"data": {"id": "ses_test1"}})
        # Обрабатываем ветвь при условии: request.url.path.endswith('/generate').
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"data": {"text": "Нейровидео готово"}})
        return httpx.Response(200, json={})

    adapter = OpenCode(service.settings, transport=httpx.MockTransport(handle))
    assert adapter.generate_text("Test")[0] == "Нейровидео готово"
    assert ("POST", "/api/session/ses_test1/interrupt") in calls
    assert ("DELETE", "/api/session/ses_test1") in calls
    adapter.client.close()


# Проверяем сценарий: rss to review to publication end to end.
def test_rss_to_review_to_publication_end_to_end(service):
    topic = service.settings.topic
    topic["channels"][0]["publication"]["mode"] = "review"
    write_topic(service.settings.root, topic)
    service.activate(Settings.load(service.settings.root))
    rss = f'<rss version="2.0"><channel><title>Video</title><item><guid>fixture</guid><title>Нейровидео</title><link>https://example.org/video</link><description>Нейросеть создала видео длительностью 12 секунд.</description><pubDate>{__import__("email.utils", fromlist=["formatdate"]).formatdate(now(), usegmt=True)}</pubDate></item></channel></rss>'.encode()
    service.http.close()
    service.http = SafeHTTP(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=rss)),
        resolver=resolver,
    )
    service.tick()
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        d = session.scalar(select(Draft))
        assert d and d.state == "review_pending"
        did, h = d.id, fingerprint(d.current)
        assert len(session.scalars(select(Material)).all()) == 1
    service.connect("main", "-123")
    service.pause("main", False)
    service.approve(did, 1, h)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as session:
        session.get(Draft, did).scheduled_at = now() - 1
    assert service.tick() == 1
    assert service.tick() == 0
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        assert len(session.scalars(select(Intent)).all()) == 1
        assert session.get(Draft, did).state == "published"


# Проверяем сценарий: cross language cluster transaction.
def test_cross_language_cluster_transaction(service):
    from max_postiz_agent.collectors import Item

    service.ingest(
        "example_rss",
        [
            Item(
                "ru",
                "Видео",
                "Нейросеть выпустила видео длиной 12 секунд.",
                "https://example.org/ru",
                now(),
                "ru",
            )
        ],
    )
    service.editor.mode = "live"
    service.settings.app["opencode"]["local_zero_cost"] = True
    service.editor.run = lambda task, context: ({"same_event": True}, {})
    service.ingest(
        "example_rss",
        [
            Item(
                "en",
                "Video",
                "A neural network released a 12 second video.",
                "https://example.org/en",
                now(),
                "en",
            )
        ],
    )
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        assert len(session.scalars(select(Material)).all()) == 2
        assert len(session.scalars(select(Story)).all()) == 1
