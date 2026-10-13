"""Файлы панели: пути, импорт, права доступа и аргументы открытия рабочего стола."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from max_postiz_agent.api import create_app
from max_postiz_agent.config import AgentError
from max_postiz_agent import panel_files


# Диалог рабочего стола не открывается при тестировании; пользовательский выбор задаёт тестовый адаптер.
def local_request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def test_browse_filters_secrets_and_stays_in_project(service, project, clip):
    (project / ".env.local").write_text("DO_NOT_LIST=true", encoding="utf-8")
    (project / "private.key").write_text("DO_NOT_LIST", encoding="utf-8")
    (project / "data/max-browser").mkdir(parents=True)
    (project / "data/max-browser/session.json").write_text("{}", encoding="utf-8")
    rows = panel_files.browse(service, purpose="video")
    names = [item["name"] for item in rows["entries"]]
    assert clip.name in names and ".env.local" not in names and "private.key" not in names
    assert rows["parent"] is None
    assert all(item["directory"] or item["path"].endswith(".mp4") for item in rows["entries"])
    assert all(item["path"] != "data/max-browser" for item in panel_files.browse(service, "data")["entries"])
    for value in ("../outside", str(project.parent / "outside"), ".env.local", "private.key", "data/max-browser"):
        with pytest.raises(AgentError):
            panel_files.browse(service, value)


def test_selection_and_import_return_portable_paths(service, project, clip, monkeypatch):
    outside = project.parent / (project.name + "-внешний ролик.mp4")
    outside.write_bytes(clip.read_bytes())
    monkeypatch.setattr(panel_files, "native_pick", lambda *args: {"state": "selected", "path": str(outside)})
    result = panel_files.choose(service, local_request(), {"purpose": "video"})
    assert result["state"] == "selected" and result["imported"]
    assert result["path"].startswith("data/media/imports/")
    assert (project / result["path"]).read_bytes() == outside.read_bytes()
    assert not Path(result["path"]).is_absolute()
    assert panel_files.selected(service, "fixture.mp4", purpose="video")["path"] == "fixture.mp4"
    with pytest.raises(AgentError, match="FILE_DIRECTORY_OUTSIDE_PROJECT"):
        panel_files.import_selected(service, project.parent, "directory", "file")


def test_workflow_selection_updates_document_and_rejects_wrong_files(service, project):
    workflow = project / "config/выбранный workflow.json"
    workflow.write_text('{"1":{"class_type":"Example","inputs":{}}}', encoding="utf-8")
    result = panel_files.selected(service, "config/выбранный workflow.json", purpose="workflow")
    assert result["document"]["1"]["class_type"] == "Example"
    with pytest.raises(AgentError, match="FILE_TYPE_INVALID"):
        panel_files.selected(service, "TOPIC.md", purpose="video")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_REQUIRED"):
        panel_files.selected(service, "TOPIC.md", kind="directory")
    workflow.write_text('{"api_key":"do-not-export"}', encoding="utf-8")
    with pytest.raises(AgentError, match="BACKUP_CONFIG_CONTAINS_SECRET"):
        panel_files.selected(service, "config/выбранный workflow.json", purpose="workflow")


# Файлы доступа не смешиваются с медиа и не раскрывают содержимое в HTTP-ответе или обычной резервной копии.
def test_private_file_import_is_separate_and_not_disclosed(service, project):
    outside = project.parent / (project.name + "-access.json")
    outside.write_text('{"access_value":"private-test-value"}', encoding="utf-8")
    result = panel_files.import_selected(service, outside, "file", "private")
    assert result["path"].startswith("data/private-imports/")
    assert "private-test-value" not in json.dumps(result)
    from max_postiz_agent.backup import runtime_files
    assert result["path"] not in dict(runtime_files(project))
    assert all(item["path"] != "data/private-imports" for item in panel_files.browse(service, "data")["entries"])


def test_cancel_remote_fallback_and_explorer_arguments(service, project, clip, monkeypatch):
    monkeypatch.setattr(panel_files, "native_pick", lambda *args: {"state": "cancelled"})
    assert panel_files.choose(service, local_request(), {}) == {"state": "cancelled"}
    remote = SimpleNamespace(client=SimpleNamespace(host="192.0.2.1"))
    assert panel_files.choose(service, remote, {}) == {"state": "unavailable"}
    with pytest.raises(AgentError, match="FILE_DESKTOP_LOCAL_ONLY"):
        panel_files.explorer(service, remote, {"path": "fixture.mp4"})
    # Проверяем аргументы Windows, не открывая настоящий Проводник из тестового процесса.
    if panel_files.os.name == "nt":
        launch = Mock()
        monkeypatch.setattr(panel_files.subprocess, "Popen", launch)
        assert panel_files.explorer(service, local_request(), {"path": "fixture.mp4"})["state"] == "explorer_opened"
        args, kwargs = launch.call_args
        assert args[0][0].lower().endswith("explorer.exe")
        assert args[0][1] == "/select," + str(clip.resolve())
        assert not kwargs.get("shell", False)
    if panel_files.os.name == "nt":
        with pytest.raises(AgentError, match="FILE_NOT_FOUND"):
            panel_files.explorer(service, local_request(), {"path": "missing.mp4"})
    else:
        with pytest.raises(AgentError, match="FILE_EXPLORER_UNAVAILABLE"):
            panel_files.explorer(service, local_request(), {"path": "fixture.mp4"})


def test_file_routes_require_owner_and_cookie_origin(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "file-owner-test-key")
    monkeypatch.setenv("APP_VIEWER_SECRET", "file-viewer-test-key")
    with TestClient(create_app(service)) as client:
        assert client.get("/api/v1/files").status_code == 401
        client.post("/api/v1/session/login", headers={"Authorization": "Bearer file-viewer-test-key"}, json={})
        assert client.get("/api/v1/files").status_code == 403
        client.post("/api/v1/session/login", headers={"Authorization": "Bearer file-owner-test-key"}, json={})
        assert client.get("/api/v1/files").status_code == 200
        assert client.post("/api/v1/files/select", json={"path": "TOPIC.md"}).status_code == 403
        response = client.post("/api/v1/files/select", headers={"Origin": "http://testserver"}, json={"path": "TOPIC.md"})
        assert response.status_code == 200 and response.json()["path"] == "TOPIC.md"
        assert "file-owner-test-key" not in json.dumps(response.json())
