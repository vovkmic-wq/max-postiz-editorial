# Авторизация, HTTP-маршруты панели и управление фоновыми обработчиками.
"""Authenticated Russian management interface and durable webhook endpoints."""

import hmac
import math
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, text

from .config import AgentError, Settings, fingerprint
from .db import (
    Approval,
    Audit,
    Campaign,
    Channel,
    Cursor,
    Draft,
    DraftRevision,
    Intent,
    Job,
    Metric,
    VideoProject,
    VideoScene,
    VideoAsset,
    Material,
    MaterialMedia,
    serialize,
)


# Создаём HTTP-приложение, защиту маршрутов и обработчики жизненного цикла.
def create_app(service, run_worker=False, legacy_image_worker=True):
    stop = threading.Event()

    # Обрабатываем очередь, пока сервер не получил сигнал остановки.
    def work():
        from .image_schedule import lock

        # Продолжаем цикл, пока выполняется условие: not stop.wait(5).
        while not stop.wait(5):
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with lock(service.settings.root, "run"):
                    service.tick()
            # Обрабатываем AgentError и выбираем допустимый исход.
            except AgentError as exc:
                # Обрабатываем ветвь при условии: exc.code != 'IMAGE_SCHEDULE_BUSY'.
                if exc.code != "IMAGE_SCHEDULE_BUSY":
                    service.config_error = "WORKER_REQUIRES_ATTENTION"
            # Обрабатываем Exception и выбираем допустимый исход.
            except Exception:
                # Never print raw exceptions from token-bearing transports.
                service.config_error = "WORKER_REQUIRES_ATTENTION"

    # Операция lifespan: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @asynccontextmanager
    async def lifespan(app):
        from .image_schedule import run_loop

        worker = threading.Thread(target=work, daemon=True) if run_worker else None
        image_worker = (threading.Thread(target=run_loop, args=(service.settings.root, stop),
                                       daemon=True, name="max-image-scheduler")
                        if run_worker and legacy_image_worker else None)
        # Обрабатываем ветвь при условии: worker.
        if worker:
            worker.start()
        # Обрабатываем ветвь при условии: image_worker.
        if image_worker:
            image_worker.start()
        yield
        stop.set()
        # Обрабатываем ветвь при условии: worker.
        if worker:
            worker.join(timeout=5)
        # Обрабатываем ветвь при условии: image_worker.
        if image_worker:
            image_worker.join(timeout=5)

    app = FastAPI(title="Агент каналов MAX", lifespan=lifespan)
    app.state.service = service

    # Операция handled: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.exception_handler(AgentError)
    async def handled(request, error):
        return JSONResponse(
            {"code": error.code, "safe_message": error.code}, status_code=error.status
        )

    # Операция limits: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.middleware("http")
    async def limits(request, call_next):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            length = int(request.headers.get("content-length", "0"))
        # Обрабатываем ValueError и выбираем допустимый исход.
        except ValueError:
            return JSONResponse({"code": "INVALID_CONTENT_LENGTH"}, status_code=400)
        # Обрабатываем ветвь при условии: length > 1048576.
        if length > 1048576:
            return JSONResponse({"code": "REQUEST_TOO_LARGE"}, status_code=413)
        # Also bound chunked requests before JSON parsing; Content-Length is not trusted.
        # Обрабатываем ветвь при условии: request.method in {'POST', 'PUT', 'PATCH'}.
        if request.method in {"POST", "PUT", "PATCH"}:
            received = bytearray()
            # Последовательно обрабатываем элементы: request.stream().
            async for chunk in request.stream():
                received.extend(chunk)
                # Обрабатываем ветвь при условии: len(received) > 1048576.
                if len(received) > 1048576:
                    return JSONResponse({"code": "REQUEST_TOO_LARGE"}, status_code=413)
            request._body = bytes(received)
        # Не передаём транспортные исключения с возможными ключами в текст ответа или лог.
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"code": "PANEL_OPERATION_FAILED"}, status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' blob:; media-src 'self' blob:; frame-ancestors 'none'"
        )
        return response

    # Подписанная сессия переживает обновление страницы; ключ в браузере не хранится.
    def auth(request: Request, authorization: str | None = Header(default=None)):
        from .panel_auth import authenticate
        return authenticate(service.settings, request, authorization)

    # Проверяем редакторскую роль или выбираем редакторский адаптер.
    def editor(role=Depends(auth)):
        # Обрабатываем ветвь при условии: role not in {'owner', 'editor'}.
        if role not in {"owner", "editor"}:
            raise AgentError("ROLE_FORBIDDEN", 403)
        return role

    # Ограничиваем операцию правами владельца.
    def owner(role=Depends(auth)):
        # Обрабатываем ветвь при условии: role != 'owner'.
        if role != "owner":
            raise AgentError("ROLE_FORBIDDEN", 403)
        return role

    # Операция rows: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    def rows(model):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session() as session:
            return [serialize(row) for row in session.scalars(select(model).limit(1000))]

    # Операция dashboard: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/")
    def dashboard():
        return FileResponse(Path(__file__).parent / "static/index.html")

    # Операция static: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/static/{name}")
    def static(name: str):
        # Обрабатываем ветвь при условии: name not in {'dashboard.js', 'style.css'}.
        if name not in {"dashboard.js", "style.css"}:
            raise AgentError("NOT_FOUND", 404)
        return FileResponse(Path(__file__).parent / "static" / name)

    # Операция live: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    # Операция ready: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/health/ready")
    def ready():
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            service.refresh()
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with service.db.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        # Обрабатываем ветвь при условии: service.config_error.
        if service.config_error:
            return JSONResponse(
                {"status": "not_ready", "code": service.config_error}, status_code=503
            )
        return {"status": "ready", "live_accepted": False}

    # Операция integrations: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/api/v1/integrations/status", dependencies=[Depends(auth)])
    def integrations():
        return {
            "opencode": service.editor.probe(),
            "max": {
                "mode": service.publisher.mode,
                "token_configured": bool(service.publisher.token),
                "live_verified": any(c["capabilities"].get("live_verified") for c in rows(Channel)),
            },
            "database": service.db.engine.dialect.name,
            "configuration": service.config_error or "valid",
            "video": service.hybrid.backend.status(),
        }

    # Начинаем сессию только после проверки предоставленного ключа.
    @app.post("/api/v1/session/login")
    def session_login(request: Request, role=Depends(auth)):
        from .panel_auth import COOKIE, LIFETIME, encode_session
        response = JSONResponse({"authenticated": True, "role": role})
        response.set_cookie(COOKIE, encode_session(service.settings, role), max_age=LIFETIME,
                            httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            path="/api")
        return response

    # Проверка входа не возвращает ни ключ, ни значение cookie.
    @app.get("/api/v1/session")
    def session_status(role=Depends(auth)):
        return {"authenticated": True, "role": role}

    # Выход удаляет браузерную сессию, не меняя ключи владельца.
    @app.post("/api/v1/session/logout")
    def session_logout(role=Depends(auth)):
        from .panel_auth import COOKIE
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/api")
        return response

    # Обновление фактических дат выполняется владельцем; блокировки источника не обходятся.
    @app.post("/api/v1/holidays/update", dependencies=[Depends(owner)])
    def holidays_update():
        from .holidays import import_catalog
        from .image_schedule import lock
        with lock(service.settings.root, "run"):
            return import_catalog(service.settings.root)

    # Включение поиска не сбрасывает суточные отметки и не повторяет праздничную попытку.
    @app.post("/api/v1/holidays/config", dependencies=[Depends(owner)])
    def holiday_options(body: dict = Body()):
        from .holidays import catalog, FILE
        from .channel_profiles import atomic_json
        from .image_schedule import lock
        if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
            raise AgentError("HOLIDAY_CONFIG_INVALID")
        with lock(service.settings.root, "run"):
            value = catalog(service.settings.root)
            value["enabled"] = body["enabled"]
            atomic_json(service.settings.root, FILE, value)
        return {"state": "saved", "enabled": body["enabled"]}

    # Календарь объединяет фактические публикации и будущие окна отправки.
    @app.get("/api/v1/calendar", dependencies=[Depends(auth)])
    def panel_calendar(channel_id: str = "test", start: str | None = None):
        from .panel_calendar import calendar
        return calendar(service, channel_id, start)

    # Недельные часы изменяет владелец; валидация выполняется до сохранения.
    @app.post("/api/v1/calendar/{channel_id}/schedule", dependencies=[Depends(owner)])
    def calendar_schedule(channel_id: str, body: dict = Body()):
        from .panel_calendar import save_schedule
        return save_schedule(service, channel_id, body)

    # Все настройки доступны в панели, без выдачи значений локальных ключей.
    @app.get("/api/v1/settings", dependencies=[Depends(owner)])
    def panel_settings():
        from .panel_settings import snapshot
        return snapshot(service)

    @app.post("/api/v1/settings/{action}", dependencies=[Depends(owner)])
    def settings_action(action: str, body: dict = Body()):
        from .panel_settings import save_configuration, save_credentials
        if action == "credentials":
            return save_credentials(service, body)
        if action in {"validate", "save"}:
            return save_configuration(service, body, validate_only=action == "validate")
        raise AgentError("NOT_FOUND", 404)

    # Выбор файлов ограничен проектом; доступ к рабочему столу получает только локальный владелец.
    @app.get("/api/v1/files", dependencies=[Depends(owner)])
    def file_list(path: str = "", purpose: str = "file"):
        from .panel_files import browse
        return browse(service, path, purpose)

    @app.post("/api/v1/files/{action}", dependencies=[Depends(owner)])
    def file_action(action: str, request: Request, body: dict = Body()):
        from .panel_files import choose, explorer, selected
        if action == "choose":
            return choose(service, request, body)
        if action == "explorer":
            return explorer(service, request, body)
        if action == "select":
            return selected(service, body.get("path", ""), body.get("kind", "file"), body.get("purpose", "file"))
        raise AgentError("NOT_FOUND", 404)

    # Возвращаем доступные профили и их состояния.
    @app.get("/api/v1/channel-profiles", dependencies=[Depends(auth)])
    def channel_profiles():
        from .channel_management import list_channels
        return list_channels(service)

    # Применяем полный профиль только с правами владельца.
    @app.post("/api/v1/channel-profiles", dependencies=[Depends(owner)])
    def channel_profile_apply(body: dict = Body()):
        from .channel_management import configure_channel
        return configure_channel(service, body)

    # Операция collections: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/api/v1/{collection}", dependencies=[Depends(auth)])
    def collections(collection: str):
        models = {
            "channels": Channel,
            "sources": Cursor,
            "drafts": Draft,
            "publications": Intent,
            "jobs": Job,
            "audit": Audit,
            "campaigns": Campaign,
            "videos": VideoProject,
            "materials": Material,
            "source-media": MaterialMedia,
            "assets": VideoAsset,
        }
        # Обрабатываем ветвь при условии: collection == 'drafts'.
        if collection == "drafts":
            return [{**row, "content_hash": fingerprint(row["current"])} for row in rows(Draft)]
        # Обрабатываем ветвь при условии: collection == 'videos'.
        if collection == "videos":
            return [{**row, "plan_hash": fingerprint(row["plan"])} for row in rows(VideoProject)]
        # Обрабатываем ветвь при условии: collection == 'channels'.
        if collection == "channels":
            return [
                {
                    **row,
                    "name": service.settings.channel(row["id"])["name"],
                    "theme": service.settings.channel(row["id"])["theme"],
                    "mode": service.settings.channel(row["id"])["publication"]["mode"],
                }
                for row in rows(Channel)
            ]
        # Обрабатываем ветвь при условии: collection not in models.
        if collection not in models:
            raise AgentError("NOT_FOUND", 404)
        return rows(models[collection])

    # Операция validate_config: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/config/validate", dependencies=[Depends(owner)])
    def validate_config():
        candidate = Settings.load(service.settings.root)
        return {
            "valid": True,
            "hash": candidate.hash,
            "changed": candidate.hash != service.settings.hash,
        }

    # Возвращаем подборку и расписание выбранного канала.
    @app.get("/api/v1/images/status", dependencies=[Depends(auth)])
    def image_status(channel_id: str = "test"):
        from .image_pipeline import latest, source_config
        from .image_schedule import status

        report = latest(service.settings.root, channel_id)
        config = (source_config(service.settings.root, channel_id)
                  if (service.settings.root / "config/image_sources.json").exists()
                  else {"sources": []})
        return {"schedule": status(service.settings.root, channel_id=channel_id), "scan_state": report["state"],
                "checked_at": report.get("checked_at"), "top": report["top"],
                "source_count": len(config["sources"]),
                "source_results": report.get("source_results", {})}

    # Управляем расписанием только выбранного канала.
    @app.post("/api/v1/images/schedule/{action}", dependencies=[Depends(owner)])
    def image_schedule_action(action: str, channel_id: str = "test"):
        from .image_schedule import configure, tick

        # Обрабатываем ветвь при условии: action == 'tick'.
        if action == "tick":
            return tick(service.settings.root, channel_id=channel_id)
        # Обрабатываем ветвь при условии: action in {'pause', 'resume'}.
        if action in {"pause", "resume"}:
            return configure(service.settings.root, action == "resume", channel_id=channel_id)
        raise AgentError("NOT_FOUND", 404)

    # Запускаем сбор без публикации под общей блокировкой отправок.
    @app.post("/api/v1/images/scan", dependencies=[Depends(owner)])
    def image_scan(body: dict = Body()):
        from .cli import load_env
        from .image_pipeline import scan
        from .image_schedule import lock

        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with lock(service.settings.root, "run"):
            load_env(service.settings.root)
            return scan(service.settings.root, all_sources=body.get("all_sources") is True,
                        exclude_used=True, channel_id=body.get("channel_id", "test"))

    # Browser login stays owner-controlled and does not approve any publication.
    # Операция browser_action: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/browser/{action}")
    def browser_action(action: str, body: dict = Body(), role=Depends(owner)):
        # Обрабатываем ветвь при условии: service.publisher.mode != 'browser'.
        if service.publisher.mode != "browser":
            raise AgentError("BROWSER_MODE_NOT_ENABLED")
        chat_id = str(body.get("chat_id", ""))
        # Обрабатываем ветвь при условии: action == 'login'.
        if action == "login":
            # При действующем входе возвращаем понятное подтверждение вместо мигающего окна.
            try:
                inspected = service.publisher.inspect(chat_id)
                if inspected.get("publish"):
                    return {"state": "already_logged_in", "chat_id": chat_id, "publish": True}
            except AgentError as exc:
                if exc.code not in {"BROWSER_LOGIN_OR_CHANNEL_REQUIRED", "BROWSER_CHANNEL_NOT_VISIBLE"}:
                    raise
            return service.publisher.login(chat_id)
        if action == "check":
            return service.publisher.inspect(chat_id)
        raise AgentError("NOT_FOUND", 404)

    # Операция create_video: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/videos")
    def create_video(body: dict = Body(), role=Depends(owner)):
        channel_id = body.get("channel_id", "main")
        # Обрабатываем ветвь при условии: 'idea' in body.
        if "idea" in body:
            return service.hybrid.generate_plan(channel_id, body["idea"], role)
        return service.hybrid.create(channel_id, body.get("plan"), role)

    # Операция video_detail: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/api/v1/videos/{project_id}", dependencies=[Depends(auth)])
    def video_detail(project_id: str):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session() as session:
            project = session.get(VideoProject, project_id)
            # Обрабатываем ветвь при условии: not project.
            if not project:
                raise AgentError("VIDEO_PROJECT_NOT_FOUND", 404)
            return {
                **serialize(project),
                "plan_hash": fingerprint(project.plan),
                "scenes": [
                    serialize(s)
                    for s in session.scalars(
                        select(VideoScene)
                        .where(VideoScene.project_id == project_id)
                        .order_by(VideoScene.index)
                    )
                ],
            }

    # Операция video_action: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/videos/{project_id}/{action}")
    def video_action(project_id: str, action: str, body: dict = Body(), role=Depends(owner)):
        # Обрабатываем ветвь при условии: action == 'approve-generation'.
        if action == "approve-generation":
            service.hybrid.approve_generation(
                project_id, body.get("revision"), body.get("plan_hash"), role
            )
            return {"queued": True}
        # Обрабатываем ветвь при условии: action == 'import-scene'.
        if action == "import-scene":
            return service.hybrid.import_scene(
                project_id,
                body.get("index"),
                body.get("path", ""),
                body.get("rights_basis", ""),
                role,
            )
        # Обрабатываем ветвь при условии: action == 'assemble'.
        if action == "assemble":
            return service.hybrid.assemble(project_id, body.get("revision"), role)
        # Обрабатываем ветвь при условии: action == 'cancel'.
        if action == "cancel":
            service.hybrid.cancel(project_id, role)
            return {"cancelled_locally": True}
        # Обрабатываем ветвь при условии: action == 'poll'.
        if action == "poll":
            service.hybrid.poll_project(project_id)
            return video_detail(project_id)
        raise AgentError("NOT_FOUND", 404)

    # Операция curate_video: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/curate")
    def curate_video(body: dict = Body(), role=Depends(owner)):
        return service.hybrid.curate(
            body.get("channel_id", "main"), body.get("media_id"), body.get("caption"), role
        )

    # Операция preview: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/api/v1/assets/{asset_id}/preview", dependencies=[Depends(auth)])
    def preview(asset_id: str):
        from .config import inside

        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session() as session:
            asset = session.get(VideoAsset, asset_id)
            # Обрабатываем ветвь при условии: not asset.
            if not asset:
                raise AgentError("VIDEO_ASSET_NOT_FOUND", 404)
            path = inside(service.settings.root, asset.path)
        return FileResponse(path, media_type="video/mp4")

    # Применяем настройки и отзываем согласования устаревших черновиков.
    @app.post("/api/v1/config/activate")
    def activate(role=Depends(owner)):
        candidate = Settings.load(service.settings.root)
        service.activate(candidate, role)
        return {"hash": candidate.hash}

    # Проверяем права адресата и сохраняем подключение профиля.
    @app.post("/api/v1/channels/{channel_id}/connect")
    def connect(channel_id: str, body: dict = Body(), role=Depends(owner)):
        return service.connect(channel_id, str(body.get("chat_id", "")), role)

    # Операция channel_action: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/channels/{channel_id}/{action}")
    def channel_action(channel_id: str, action: str, role=Depends(owner)):
        # Обрабатываем ветвь при условии: action == 'bootstrap'.
        if action == "bootstrap":
            return service.model_task(
                "bootstrap", channel_id, {"channel": service.settings.channel(channel_id)}
            )
        # Обрабатываем ветвь при условии: action == 'growth-plan'.
        if action == "growth-plan":
            return service.growth_plan(channel_id)
        # Обрабатываем ветвь при условии: action not in {'pause', 'resume'}.
        if action not in {"pause", "resume"}:
            raise AgentError("NOT_FOUND", 404)
        service.pause(channel_id, action == "pause", role)
        return {"paused": action == "pause"}

    # Собираем материалы источника с учётом курсора и ограничений.
    @app.post("/api/v1/sources/{source_id}/collect")
    def collect(source_id: str, role=Depends(owner)):
        source = service.settings.source(source_id)
        # Обрабатываем ветвь при условии: not source['enabled'].
        if not source["enabled"]:
            raise AgentError("SOURCE_DISABLED")
        import uuid

        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session.begin() as session:
            service.enqueue(session, "collect", source_id, 0, f"manual:{source_id}:{uuid.uuid4()}")
            service.db.audit(session, role, "collect_requested", source_id)
        return {"queued": True}

    # Проверяем согласование текущей ревизии материала.
    @app.post("/api/v1/drafts/{draft_id}/approve")
    def approve(draft_id: str, body: dict = Body(), role=Depends(editor)):
        service.approve(draft_id, body.get("revision"), body.get("content_hash"), role)
        return {"scheduled": True}

    # Предпросмотр использует проверенный локальный файл, без доступа к чужим каталогам.
    @app.get("/api/v1/drafts/{draft_id}/image", dependencies=[Depends(auth)])
    def draft_image(draft_id: str):
        from .media import inspect_image
        from .config import inside
        with service.db.session() as session:
            draft = session.get(Draft, draft_id)
            image = draft.current.get("image", {}) if draft else {}
            if not image:
                raise AgentError("IMAGE_NOT_FOUND", 404)
            metadata = inspect_image(service.settings.root, image["path"])
            if metadata["sha256"] != image["sha256"]:
                raise AgentError("IMAGE_HASH_CHANGED")
            return FileResponse(inside(service.settings.root, image["path"]))

    # Создаём новую ревизию вместо изменения согласованного содержимого.
    @app.post("/api/v1/drafts/{draft_id}/revise")
    def revise(draft_id: str, body: dict = Body(), role=Depends(editor)):
        service.revise(draft_id, body.get("revision"), body.get("body", ""), role)
        return {"revised": True}

    # Операция video: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/drafts/{draft_id}/video")
    def video(draft_id: str, body: dict = Body(), role=Depends(owner)):
        from .media import attach_video

        return attach_video(
            service, draft_id, body.get("path", ""), body.get("rights_basis", ""), role
        )

    # Операция commercial: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/drafts/{draft_id}/commercial")
    def commercial(draft_id: str, body: dict = Body(), role=Depends(owner)):
        from sqlalchemy import update

        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session.begin() as session:
            draft = session.get(Draft, draft_id)
            # Обрабатываем ветвь при условии: not draft or draft.revision != body.get('revision') or draft.state == 'published' or session.scalar(select(….
            if (
                not draft
                or draft.revision != body.get("revision")
                or draft.state == "published"
                or session.scalar(select(Intent).where(Intent.draft_id == draft_id))
            ):
                raise AgentError("DRAFT_REVISION_CONFLICT", 409)
            draft.commercial = body.get("commercial") is True
            draft.state, draft.scheduled_at = "review_pending", None
            session.execute(
                update(Approval).where(Approval.draft_id == draft_id).values(revoked=True)
            )
            service.db.audit(session, role, "commercial_tag", draft_id, commercial=draft.commercial)
        return {"commercial": draft.commercial}

    # Отклоняем материал и прекращаем его продвижение к отправке.
    @app.post("/api/v1/drafts/{draft_id}/reject")
    def reject(draft_id: str, body: dict = Body(), role=Depends(editor)):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session.begin() as session:
            draft = session.get(Draft, draft_id)
            # Обрабатываем ветвь при условии: not draft or draft.revision != body.get('revision') or draft.state == 'published'.
            if not draft or draft.revision != body.get("revision") or draft.state == "published":
                raise AgentError("DRAFT_REVISION_CONFLICT", 409)
            draft.state = "rejected"
            service.db.audit(session, role, "reject", draft_id)
        return {"rejected": True}

    # Сверяем сохранённую попытку без повторного нажатия отправки.
    @app.post("/api/v1/publications/{intent_id}/reconcile", dependencies=[Depends(owner)])
    def reconcile(intent_id: str):
        return {"state": service.reconcile(intent_id)}

    # Операция correct: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/publications/{intent_id}/correct")
    def correct(intent_id: str, body: dict = Body(), role=Depends(editor)):
        # Обрабатываем ветвь при условии: service.publisher.mode == 'browser' and (not body.get('remove')).
        if service.publisher.mode == "browser" and not body.get("remove"):
            # Обрабатываем ветвь при условии: role != 'owner'.
            if role != "owner":
                raise AgentError("ROLE_FORBIDDEN", 403)
            from .browser_corrections import correct_caption

            return correct_caption(service, intent_id, body.get("revision"), body.get("body", ""))
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session.begin() as session:
            intent = session.get(Intent, intent_id)
            # Обрабатываем ветвь при условии: not intent or intent.state != 'confirmed'.
            if not intent or intent.state != "confirmed":
                raise AgentError("PUBLICATION_NOT_CONFIRMED", 409)
            draft = session.get(Draft, intent.draft_id)
            # Обрабатываем ветвь при условии: body.get('revision') != draft.revision.
            if body.get("revision") != draft.revision:
                raise AgentError("DRAFT_REVISION_CONFLICT", 409)
            remove = body.get("remove") is True
            text_value = body.get("body", "")
            # Обрабатываем ветвь при условии: not remove.
            if not remove:
                materials = service.db.export()["materials"]
                from .editorial import validate_draft

                candidate = {k: v for k, v in draft.current.items() if k not in {"checks", "video"}}
                candidate["body"] = text_value
                checks = validate_draft(
                    candidate,
                    [m for m in materials if m["story_id"] == draft.story_id],
                    service.settings.channel(draft.channel_id),
                    allow_original=session.scalar(
                        select(VideoProject).where(VideoProject.draft_id == draft.id)
                    )
                    is not None,
                )
                # Обрабатываем ветвь при условии: set(checks) - {'EDITOR_REVIEW_REQUIRED'}.
                if set(checks) - {"EDITOR_REVIEW_REQUIRED"}:
                    raise AgentError("QUALITY_GATE_BLOCKED")
            service.publisher.correct(intent.external_id, text_value, remove)
            intent.state = "removed" if remove else "confirmed"
            # Обрабатываем ветвь при условии: not remove.
            if not remove:
                # Обрабатываем ветвь при условии: 'video' in draft.current.
                if "video" in draft.current:
                    candidate["video"] = draft.current["video"]
                candidate["checks"] = checks
                draft.current = candidate
                draft.revision += 1
                intent.body_hash = fingerprint(text_value)
                session.add(
                    DraftRevision(
                        draft_id=draft.id,
                        revision=draft.revision,
                        payload=candidate,
                        config_id=draft.config_id,
                    )
                )
            service.db.audit(session, role, "remove" if remove else "correct", intent_id)
        return {"confirmed": True}

    # Операция report: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.get("/api/v1/reports/{channel_id}", dependencies=[Depends(auth)])
    def report(channel_id: str):
        return service.report(channel_id)

    # Операция metric: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/api/v1/metrics")
    def metric(body: dict = Body(), role=Depends(owner)):
        channel_id = body.get("channel_id")
        service.settings.channel(channel_id)
        # Обрабатываем ветвь при условии: body.get('name') not in {'subscribers', 'views', 'clicks', 'revenue_rub', 'spend_rub'}.
        if body.get("name") not in {"subscribers", "views", "clicks", "revenue_rub", "spend_rub"}:
            raise AgentError("METRIC_INVALID")
        # Обрабатываем ветвь при условии: isinstance(body.get('value'), bool) or not isinstance(body.get('value'), (int, float)) or (not math.isfinit….
        if (
            isinstance(body.get("value"), bool)
            or not isinstance(body.get("value"), (int, float))
            or not math.isfinite(body["value"])
            or body["value"] < 0
        ):
            raise AgentError("METRIC_INVALID")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with service.db.session.begin() as session:
            session.add(
                Metric(
                    channel_id=channel_id, name=body["name"], value=body["value"], origin="manual"
                )
            )
            service.db.audit(session, role, "metric_import", channel_id)
        return {"saved": True}

    # Операция webhook: авторизация, http-маршруты панели и управление фоновыми обработчиками.
    @app.post("/webhooks/{platform}")
    async def webhook(platform: str, request: Request):
        # Обрабатываем ветвь при условии: platform not in {'max', 'telegram'}.
        if platform not in {"max", "telegram"}:
            raise AgentError("NOT_FOUND", 404)
        env = "MAX_WEBHOOK_SECRET" if platform == "max" else "TELEGRAM_WEBHOOK_SECRET"
        header = "X-Max-Bot-Api-Secret" if platform == "max" else "X-Telegram-Bot-Api-Secret-Token"
        expected = os.getenv(env, "")
        supplied = request.headers.get(header, "")
        # Обрабатываем ветвь при условии: not expected or not hmac.compare_digest(expected, supplied).
        if not expected or not hmac.compare_digest(expected, supplied):
            raise AgentError("WEBHOOK_FORBIDDEN", 403)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            payload = await request.json()
        # Обрабатываем ValueError и выбираем допустимый исход.
        except ValueError as exc:
            raise AgentError("INVALID_EVENT") from exc
        # Обрабатываем ветвь при условии: not isinstance(payload, dict).
        if not isinstance(payload, dict):
            raise AgentError("INVALID_EVENT")
        accepted = service.receive(platform, payload)
        return {"accepted": accepted}

    return app
