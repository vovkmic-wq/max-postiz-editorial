"""Проверяем изоляцию каналов, смену тематики и восстановление в другом каталоге."""

import copy
import hashlib
import io
import shutil
import zipfile
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

from max_postiz_agent import image_pipeline as images, image_schedule as schedule
from max_postiz_agent.api import create_app
from max_postiz_agent.backup import backup, restore
from max_postiz_agent.channel_management import configure_channel
from max_postiz_agent.channel_profiles import PROFILE_FILE, atomic_json, profiles
from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.db import Channel, Database, Draft, Intent
from max_postiz_agent.portability import package_project
from max_postiz_agent.service import Service


# Операция document: данного модуля.
def document(service, channel_id="nature"):
    profile = copy.deepcopy(service.settings.channel("test"))
    profile.update(id=channel_id, enabled=True, name="Природа", theme="Живая природа",
                   audience="Любители природы", include_topics=["природа"],
                   content_pillars=["Животные", "Пейзажи"])
    profile["publication"]["mode"] = "review"
    return {"channel": profile, "images": {
        "queries": ["nature", "wildlife"], "image_type": "photo", "interval_hours": 6,
        "local_prompts": ["Beautiful nature and wildlife photography", "Text or computer screenshot"],
        "caption_title": "Природа в объективе", "filters": {"min_views": 0, "min_likes": 0},
    }}


# Проверяем сценарий: channel theme controls search model caption and schedule.
def test_channel_theme_controls_search_model_caption_and_schedule(service, project):
    configure_channel(service, document(service))
    nature = images.source_config(project, "nature")
    original = images.source_config(project)
    assert nature["sources"][0]["queries"] == ["nature", "wildlife"]
    assert nature["sources"][0]["image_type"] == "photo"
    assert nature["local_prompts"][0].startswith("Beautiful nature")
    assert original["sources"][0]["queries"] != nature["sources"][0]["queries"]
    assert nature["caption_title"] == "Природа в объективе"
    schedule.configure(project, True, at=datetime(2026, 10, 11, 9, tzinfo=schedule.MOSCOW), channel_id="nature")
    assert schedule.status(project, channel_id="nature")["hours"] == [0, 6, 12, 18]
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as db:
        assert db.get(Channel, "nature").paused


# Проверяем сценарий: reports and staged images belong to their channel.
def test_reports_and_staged_images_belong_to_their_channel(service, project):
    configure_channel(service, document(service))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as db:
        db.add(Draft(channel_id="test", story_id="existing", config_id=service.settings.hash,
                     current={"image": {"source_id": "one", "source_image_id": "123",
                                        "page_url": "https://example.org/one", "source_sha256": "hash"}}))
    assert ("one", "123") in images.used_images(project)["ids"]
    assert images.used_images(project, "nature")["ids"] == set()
    atomic_json(project, "data/image-ranking/latest.json", {"state": "ready", "top": [{"rank": 1}]})
    assert images.latest(project, "nature")["state"] == "not_scanned"


# Проверяем сценарий: changed theme invalidates selection and pauses its schedule.
def test_changed_theme_invalidates_selection_and_pauses_its_schedule(service, project):
    configure_channel(service, document(service))
    cfg = images.source_config(project, "nature")
    path = project / "data/image-ranking/files/nature.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (512, 512), "green").save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json(project, "data/image-ranking/channels/nature/latest.json", {
        "state": "ready", "top": [{"rank": 1, "path": path.relative_to(project).as_posix(), "sha256": sha}],
        "checked_at": datetime.now(timezone.utc).isoformat(), "profile_hash": cfg["profile_hash"],
    })
    assert images.selected(project, 1, sha, "nature")["sha256"] == sha
    next_profile = document(service)
    next_profile["channel"]["theme"] = "Космос"
    configure_channel(service, next_profile)
    assert schedule.status(project, channel_id="nature")["state"] == "paused"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="IMAGE_TOPIC_CHANGED"):
        images.selected(project, 1, sha, "nature")


# Проверяем сценарий: one channel error does not stop another.
def test_one_channel_error_does_not_stop_another(service, project, monkeypatch):
    configure_channel(service, document(service))
    start = datetime(2026, 10, 11, 9, tzinfo=schedule.MOSCOW)
    # Последовательно обрабатываем элементы: ('test', 'nature').
    for channel in ("test", "nature"):
        schedule.configure(project, True, at=start, channel_id=channel)
    calls = []
    # Операция run: данного модуля.
    def run(root, at, *, channel_id="test"):
        calls.append(channel_id)
        return {"state": "error", "code": "EXPECTED"} if channel_id == "nature" else {"state": "confirmed"}
    monkeypatch.setattr(schedule, "run_images", run)
    results = schedule.tick_all(project, at=start.replace(hour=12))
    assert results["nature"]["schedule_state"] == "requires_attention"
    assert results["test"]["state"] == "confirmed" and len(calls) == 2
    results = schedule.tick_all(project, at=start.replace(hour=16))
    assert results["nature"]["state"] == "requires_attention"
    assert results["test"]["state"] == "confirmed"


# Проверяем сценарий: profile change refuses unknown delivery and invalid input is atomic.
def test_profile_change_refuses_unknown_delivery_and_invalid_input_is_atomic(service, project):
    before = (project / "TOPIC.md").read_bytes()
    bad = document(service)
    bad["images"]["interval_hours"] = 5
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="CONFIG_SCHEMA_INVALID"):
        configure_channel(service, bad)
    assert (project / "TOPIC.md").read_bytes() == before
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as db:
        db.add(Intent(channel_id="test", draft_id="d", body_hash="h", state="unknown"))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="UNRESOLVED_PUBLICATION"):
        configure_channel(service, document(service, "test"))
    assert not (project / PROFILE_FILE).exists()


# Проверяем сценарий: api channel selection and owner only profile changes.
def test_api_channel_selection_and_owner_only_profile_changes(service, project, monkeypatch):
    configure_channel(service, document(service))
    monkeypatch.setenv("APP_AUTH_SECRET", "owner-test-key")
    monkeypatch.setenv("APP_VIEWER_SECRET", "viewer-test-key")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service)) as client:
        headers = {"Authorization": "Bearer viewer-test-key"}
        listed = client.get("/api/v1/channel-profiles", headers=headers)
        assert listed.status_code == 200 and any(row["channel"]["id"] == "nature" for row in listed.json())
        legacy = next(row for row in listed.json() if row["channel"]["id"] == "test")
        assert legacy["images"]["queries"] and legacy["images"]["local_prompts"]
        response = client.get("/api/v1/images/status?channel_id=nature", headers=headers)
        assert response.status_code == 200 and response.json()["schedule"]["channel_id"] == "nature"
        assert client.post("/api/v1/channel-profiles", headers=headers, json=document(service)).status_code == 403
        assert client.get("/api/v1/images/status?channel_id=../secret", headers=headers).status_code == 422


# Проверяем сценарий: restore in another directory preserves history without credentials.
def test_restore_in_another_directory_preserves_history_without_credentials(service, project, tmp_path):
    configure_channel(service, document(service))
    atomic_json(project, "data/image-schedule/control.json", {"enabled": True})
    atomic_json(project, "data/image-schedule/channels/nature/control.json", {"enabled": True})
    atomic_json(project, "data/image-ranking/auto-runs/2026-10-11-08.json", {"state": "confirmed"})
    atomic_json(project, "data/browser-publications/one.json", {"state": "unknown"})
    (project / ".env.local").write_text("PIXABAY_API_KEY=secret-marker", encoding="utf-8")
    (project / "data/max-browser").mkdir()
    (project / "data/max-browser/Cookies").write_text("secret-cookie", encoding="utf-8")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as db:
        db.add(Intent(channel_id="nature", draft_id="d", body_hash="h", state="sending"))
    key, archive = Fernet.generate_key(), tmp_path / "relocate.enc"
    backup(service, archive, key)
    clear = Fernet(key).decrypt(archive.read_bytes())
    assert b"secret-marker" not in clear and b"secret-cookie" not in clear
    target_root = tmp_path / "different-computer"
    # Последовательно обрабатываем элементы: ('schemas', 'prompts', 'config').
    for folder in ("schemas", "prompts", "config"):
        shutil.copytree(project / folder, target_root / folder)
    shutil.copy2(project / "TOPIC.md", target_root / "TOPIC.md")
    settings = Settings.load(target_root)
    db = Database(settings)
    db.initialize()
    target = Service(settings, db)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        restore(target, archive, key)
        assert profiles(target_root)["channels"]["nature"]["queries"] == ["nature", "wildlife"]
        assert not schedule.read(target_root, "control", {})["enabled"]
        assert not schedule.read(target_root, "control", {}, "nature")["enabled"]
        assert (target_root / "data/image-ranking/auto-runs/2026-10-11-08.json").exists()
        assert not (target_root / ".env.local").exists()
        assert not (target_root / "data/max-browser").exists()
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with db.session() as session:
            assert all(row.paused for row in session.query(Channel))
            assert session.query(Intent).one().state == "unknown"
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        target.close()


# Проверяем сценарий: source package contains no runtime or local secrets.
def test_source_package_contains_no_runtime_or_local_secrets(project, tmp_path):
    # Упаковка использует явный список путей даже при неизвестных локальных файлах.
    (project / ".env.local").write_text("secret-marker", encoding="utf-8")
    output = tmp_path / "project.zip"
    result = package_project(project, output)
    assert result["state"] == "packaged"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with zipfile.ZipFile(io.BytesIO(output.read_bytes())) as archive:
        assert "package-manifest.json" in archive.namelist()
        assert not any(name.startswith(("data/", ".env", ".venv/")) for name in archive.namelist())


# Две независимые редакции публикуют в разные адресаты и возвращаются на паузу.
def test_two_channels_publish_to_their_own_addresses(service, project, monkeypatch):
    from test_browser_publishing import browser_config
    from max_postiz_agent import openverse_source
    from max_postiz_agent.adapters import Delivery
    from max_postiz_agent.config import fingerprint

    # Настраиваем оба профиля до подключения, чтобы подтверждения прав были актуальны.
    service.activate(browser_config(project))
    for channel_id, chat_id in (("nature", "-111"), ("space", "-222")):
        profile = document(service, channel_id)
        profile.update(chat_id=chat_id, chat_title=channel_id)
        configure_channel(service, profile)
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    # Последовательно обрабатываем элементы: (('nature', '-111'), ('space', '-222')).
    for channel_id, chat_id in (("nature", "-111"), ("space", "-222")):
        service.connect(channel_id, chat_id)
    path = project / "data/image-ranking/files/shared.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (512, 512), "purple").save(path)
    content, sent = path.read_bytes(), []
    sha = hashlib.sha256(content).hexdigest()
    source = next(row for row in images.source_config(project)["sources"] if row["type"] == "openverse_source")
    row = {"rank": 1, "source_id": source["id"], "id": "9ff3dd33-0705-46d9-97a6-0da64fe227dd",
           "sha256": sha, "path": path.relative_to(project).as_posix(), "author": "Artist",
           "page_url": "https://example.org/nature", "image_url": "https://example.org/nature.png",
           "license": "CC BY", "license_url": "https://creativecommons.org/licenses/by/4.0/",
           "metrics_available": False, "views": 0, "likes": 0}
    monkeypatch.setattr(openverse_source, "verify", lambda *a: (row, None))
    monkeypatch.setattr(openverse_source, "image_bytes", lambda *a: content)
    # Выполняем отправку и возвращаем подтверждённый либо неопределённый результат.
    def publish(chat, body, attachment=None, *, request_id=None):
        sent.append((chat, attachment["payload"]["sha256"]))
        return Delivery("confirmed", external_id=chat + "-post")
    monkeypatch.setattr(service.publisher, "publish", publish)
    # Одинаковый файл не смешивает журнал одного канала с журналом другого.
    for channel_id in ("nature", "space"):
        cfg = images.source_config(project, channel_id)
        atomic_json(project, f"data/image-ranking/channels/{channel_id}/latest.json", {
            "state": "ready", "top": [row], "checked_at": datetime.now(timezone.utc).isoformat(),
            "profile_hash": cfg["profile_hash"],
        })
        assert images.publish_rank(service, 1, sha, channel_id=channel_id)["state"] == "confirmed"
    assert sent == [("-111", sha), ("-222", sha)]
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as db:
        assert db.get(Channel, "nature").paused and db.get(Channel, "space").paused


# Частота вычисляется в часовом поясе канала, а не в поясе компьютера.
def test_channel_timezone_is_independent_of_host_timezone(service, project):
    profile = document(service)
    profile["channel"]["publication"]["timezone"] = "UTC"
    configure_channel(service, profile)
    at = datetime(2026, 10, 11, 10, tzinfo=schedule.MOSCOW)
    result = schedule.configure(project, True, at=at, channel_id="nature")
    assert result["next_run_at"] == "2026-10-11T12:00:00+00:00"
    assert result["timezone"] == "UTC"
