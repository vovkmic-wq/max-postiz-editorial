"""Authenticated hub API layered on the existing MAX panel and session implementation."""
from pathlib import Path

from fastapi import Depends, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .api import create_app
from .config import AgentError, fingerprint
from .db import serialize
from .hub_models import Dispatch, DispatchAttempt, RecurringTask, Target, Variant, VariantRevision
from .panel_auth import authenticate


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionInput(Input):
    revision: int = Field(ge=1)
    body: str = Field(min_length=1)
    title: str | None = None
    media: list[dict] | None = None
    metadata: dict | None = None


class ApprovalInput(Input):
    revision: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    due_at: float = Field(ge=0, allow_inf_nan=False)


class PauseInput(Input):
    paused: StrictBool


class AttestationInput(Input):
    revision: int = Field(ge=1)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    url: str


class OriginalInput(Input):
    revision: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)


class BridgeConnectInput(Input):
    target_id: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=16, max_length=128)
    verifier: str = Field(min_length=16, max_length=128)


class BridgeExchangeInput(Input):
    code: str = Field(min_length=1, max_length=4096)
    verifier: str = Field(min_length=16, max_length=128)


class BridgePostInput(Input):
    channel_token: str = Field(min_length=1, max_length=4096)
    integration_id: str = Field(min_length=1, max_length=128)
    post_id: str = Field(min_length=1, max_length=128)


class BridgeMutationInput(Input):
    identifiers: list[str] = Field(max_length=100)
    owner: str | None = Field(default=None, max_length=36)


class BridgeUnlockInput(Input):
    confirm_no_request_inflight: StrictBool


class RedditSourceInput(Input):
    title: str = Field(min_length=1, max_length=500)
    original: str = Field(default="", max_length=8500)
    url: str = Field(min_length=1, max_length=2000)
    target_id: str = Field(min_length=1, max_length=64)


class RedditTextInput(Input):
    title: str = Field(min_length=1, max_length=500)
    original: str = Field(min_length=1, max_length=8500)


class RedditRightsInput(Input):
    evidence: str = Field(min_length=1, max_length=1000)
    source_hash: str = Field(min_length=64, max_length=64)


class RedditMailInput(Input):
    username: str = Field(min_length=1, max_length=254)
    app_password: str = Field(default="", max_length=64)
    enabled: StrictBool


def hub_app(service, run_worker=False):
    app = create_app(service, run_worker=run_worker, legacy_image_worker=False)
    # The old scheduler creates a base MAX Service and would bypass target approvals.
    app.router.routes = [route for route in app.router.routes
                         if getattr(route, "path", "") != "/api/v1/images/schedule/{action}"]
    app.title = "Редакция MAX / Пикабу / Postiz"

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Pydantic's default detail includes submitted values, including credentials.
        return JSONResponse({"code": "INPUT_INVALID"}, status_code=422)
    assets = Path(__file__).parent / "hub_static"
    app.mount("/hub-assets", StaticFiles(directory=assets), name="hub-assets")

    def auth(request: Request, authorization: str | None = Header(default=None)):
        return authenticate(service.settings, request, authorization)

    def editor(role=Depends(auth)):
        if role not in {"owner", "editor"}:
            raise AgentError("ROLE_FORBIDDEN", 403)
        return role

    def owner(role=Depends(auth)):
        if role != "owner":
            raise AgentError("ROLE_FORBIDDEN", 403)
        return role

    @app.get('/api/hub/owner-browser-authorize', status_code=204)
    def owner_browser_authorize(role=Depends(owner)):
        """Nginx auth_request gate for the private Linux owner display."""
        return Response(status_code=204)

    @app.get("/api/hub/reddit")
    def reddit_inbox(role=Depends(auth)):
        rows = service.reddit_intake.rows()
        for row in rows:
            row["source_hash"] = fingerprint({k: row[k] for k in ("title", "original", "url")})
        mail = service.reddit_mail.status()
        if role == "owner":
            mail["username"] = service.reddit_mail.read()["username"]
        return {"items": rows, "mail": mail,
                "options": service.reddit_intake.options(), "automatic_publication": False}

    @app.post("/api/hub/reddit/items")
    def reddit_submit(value: RedditSourceInput, role=Depends(editor)):
        return service.reddit_intake.submit(**value.model_dump(), actor=role)

    @app.post("/api/hub/reddit/items/{ident}/text")
    def reddit_text(ident: str, value: RedditTextInput, role=Depends(editor)):
        return service.reddit_intake.edit(ident, value.title, value.original, role)

    @app.post("/api/hub/reddit/items/{ident}/rights")
    def reddit_rights(ident: str, value: RedditRightsInput, role=Depends(owner)):
        return service.reddit_intake.attest_rights(ident, value.evidence, value.source_hash, role)

    @app.post("/api/hub/reddit/items/{ident}/resume")
    def reddit_resume(ident: str, role=Depends(owner)):
        return service.reddit_intake.resume(ident)

    @app.post("/api/hub/reddit/process")
    def reddit_process(role=Depends(owner)):
        from .image_schedule import lock
        with lock(service.settings.root, "run"):
            return {"prepared": service.reddit_intake.process_one(), "external_posts": 0}

    @app.post("/api/hub/reddit/mail")
    def reddit_mail(value: RedditMailInput, role=Depends(owner)):
        from .image_schedule import lock
        with lock(service.settings.root, "run"):
            return service.reddit_mail.configure(**value.model_dump())

    @app.get("/api/hub/reddit/mail/template")
    def reddit_mail_template(role=Depends(owner)):
        return service.reddit_mail.template()

    @app.post("/api/hub/reddit/mail/poll")
    def reddit_mail_poll(role=Depends(owner)):
        from .image_schedule import lock
        with lock(service.settings.root, "run"):
            return {"accepted": service.reddit_mail.poll()}

    def bridge_auth(x_max_bridge_key: str | None = Header(default=None)):
        service.bridge.authorize_request(x_max_bridge_key)

    @app.post("/api/max-bridge/connect")
    def bridge_connect(value: BridgeConnectInput, access=Depends(bridge_auth)):
        return service.bridge.connect(value.target_id, value.state, value.verifier)

    @app.post("/api/max-bridge/exchange")
    def bridge_exchange(value: BridgeExchangeInput, access=Depends(bridge_auth)):
        return service.bridge.exchange(value.code, value.verifier)

    @app.post("/api/max-bridge/submit")
    def bridge_submit(value: BridgePostInput, access=Depends(bridge_auth)):
        return service.bridge.submit(value.channel_token, value.integration_id, value.post_id)

    @app.post("/api/max-bridge/status")
    def bridge_status(value: BridgePostInput, access=Depends(bridge_auth)):
        return service.bridge.status(value.channel_token, value.integration_id, value.post_id)

    @app.post("/api/max-bridge/mutation/start")
    def bridge_mutation_start(value: BridgeMutationInput, access=Depends(bridge_auth)):
        return service.bridge.mutation(value.identifiers)

    @app.post("/api/max-bridge/mutation/finish")
    def bridge_mutation_finish(value: BridgeMutationInput, access=Depends(bridge_auth)):
        if not value.owner:
            raise AgentError("BRIDGE_MUTATION_OWNER_REQUIRED")
        return service.bridge.mutation(value.identifiers, value.owner)

    @app.post("/api/hub/max-bridge/sync")
    def bridge_sync(role=Depends(editor)):
        return {"imported": service.bridge.sync(force=True)}

    @app.post("/api/hub/max-bridge/unlock/{post_id}")
    def bridge_unlock(post_id: str, value: BridgeUnlockInput, role=Depends(owner)):
        return service.bridge.unlock(post_id, value.confirm_no_request_inflight)

    @app.post("/api/hub/variants/{ident}/attest-original")
    def bridge_original(ident: str, value: OriginalInput, role=Depends(owner)):
        return service.bridge.attest_original(ident, value.revision, value.content_hash, role)

    @app.get("/hub")
    def hub_page():
        return FileResponse(assets / "index.html")

    @app.get("/api/hub/state")
    def state(role=Depends(auth)):
        with service.db.session() as s:
            variants = []
            for v in s.scalars(select(Variant).order_by(Variant.updated_at.desc()).limit(200)):
                t = s.get(Target, v.target_id)
                value = serialize(v)
                value["content_hash"] = fingerprint(service.snapshot(v, t))
                variants.append(value)
            return {"targets": [serialize(t) for t in s.scalars(select(Target))], "variants": variants,
                    "role": role,
                    "tasks": [serialize(t) for t in s.scalars(select(RecurringTask))],
                    "dispatches": [serialize(d) for d in s.scalars(select(Dispatch).order_by(Dispatch.due_at.desc()).limit(200))],
                    "postiz_configured": bool(service.postiz.base and service.postiz.key),
                    "max_bridge": {"enabled": service.bridge.enabled, "error": service.bridge.last_error},
                    "model": service.editor.model, "model_mode": service.editor.mode,
                    "database": service.db.engine.dialect.name}

    @app.get("/api/hub/archive")
    def archive(q: str, role=Depends(auth)):
        return service.search_archive(q)

    @app.get("/api/hub/variants/{ident}/history")
    def history(ident: str, role=Depends(auth)):
        with service.db.session() as s:
            return [serialize(v) for v in s.scalars(select(VariantRevision).where(VariantRevision.variant_id == ident).order_by(VariantRevision.revision))]

    @app.post("/api/hub/drafts/{ident}/variants")
    def variants(ident: str, role=Depends(editor)):
        return {"variant_ids": service.make_variants(ident)}

    @app.post("/api/hub/variants/{ident}/revise")
    def revise(ident: str, value: RevisionInput, role=Depends(editor)):
        return service.revise_variant(ident, value.revision, value.body, value.title, value.media, role, value.metadata)

    @app.post("/api/hub/variants/{ident}/approve")
    def approve(ident: str, value: ApprovalInput, role=Depends(editor)):
        return {"dispatch_id": service.approve_variant(ident, value.revision, value.content_hash, value.due_at, role)}

    @app.post("/api/hub/variants/{ident}/adapt")
    def adapt(ident: str, role=Depends(editor)):
        return service.adapt_variant(ident)

    @app.post("/api/hub/variants/{ident}/postiz")
    def stage(ident: str, role=Depends(editor)):
        return {"external_id": service.stage_remote(ident)}

    @app.post("/api/hub/variants/{ident}/sync")
    def sync(ident: str, role=Depends(editor)):
        return service.sync_remote(ident)

    @app.post("/api/hub/targets/{ident}/pause")
    def pause(ident: str, value: PauseInput, role=Depends(owner)):
        service.pause_target(ident, value.paused, role)
        return {"paused": value.paused}

    @app.post("/api/hub/dispatches/{ident}/reconcile")
    def reconcile(ident: str, role=Depends(owner)):
        return service.reconcile_dispatch(ident)

    @app.post("/api/hub/dispatches/{ident}/cancel")
    def cancel(ident: str, role=Depends(editor)):
        return service.cancel_dispatch(ident)

    @app.post("/api/hub/dispatches/{ident}/attest")
    def attest(ident: str, value: AttestationInput, role=Depends(owner)):
        return service.attest_manual(ident, value.revision, value.snapshot_hash, value.url, role)

    @app.post("/api/hub/media")
    async def upload_media(file: UploadFile = File(...), role=Depends(editor)):
        import hashlib
        from .config import inside
        types = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}
        if file.content_type not in types:
            raise AgentError("MEDIA_TYPE_UNSUPPORTED")
        directory = inside(service.settings.root, service.settings.app["media_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        from .db import uid
        path = directory / (uid() + types[file.content_type])
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("wb") as destination:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise AgentError("MEDIA_TOO_LARGE", 413)
                    digest.update(chunk)
                    destination.write(chunk)
            media = {"path": path.relative_to(service.settings.root).as_posix(),
                     "sha256": digest.hexdigest(), "mime": file.content_type}
            from types import SimpleNamespace
            service.check_media([media], SimpleNamespace(route="manual"))
            return media
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get("/api/hub/dispatches/{ident}/attempts")
    def attempts(ident: str, role=Depends(auth)):
        with service.db.session() as s:
            return [serialize(a) for a in s.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == ident))]

    @app.get("/api/hub/dispatches/{ident}/export/{name}")
    def exported(ident: str, name: str, role=Depends(editor)):
        from .config import inside
        if name not in {"post.json", "post.txt", "post.html"}:
            raise AgentError("NOT_FOUND", 404)
        with service.db.session() as s:
            d = s.get(Dispatch, ident)
            if not d or d.state != "awaiting_manual":
                raise AgentError("EXPORT_NOT_CURRENT", 409)
        path = inside(service.settings.root, "data/exports/" + d.id + "/" + name)
        if not path.is_file():
            raise AgentError("NOT_FOUND", 404)
        return FileResponse(path, filename=name)

    @app.post("/api/hub/tick")
    def tick(role=Depends(owner)):
        return {"published": service.tick()}

    @app.post("/api/hub/sources/{ident}/resume")
    def resume_source(ident: str, role=Depends(owner)):
        service.resume_source(ident)
        return {"state": "resumed"}

    @app.post("/api/hub/tasks/{ident}/resume")
    def resume_task(ident: str, role=Depends(owner)):
        service.resume_task(ident, role)
        return {"state": "resumed"}

    return app
