"""Private mobile API. Domain state and platform effects remain server-owned."""
import base64
import json
import logging
import math
import re
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import __version__
from .config import AgentError
from .db import now
from .mobile_auth import ID_PATTERN, MobilePrincipal
from .mobile_store import MobileJournal, safe_code, timestamp

logger = logging.getLogger(__name__)
OBJECT_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
Action = Literal[
    "pause_project", "resume_project", "pause_target", "resume_target",
    "hold_item", "release_item_hold", "reconcile_dispatch", "retire_preapproval_no_send",
]
READS = frozenset({"overview", "reserve", "targets", "queue", "calendar"})
DETAILS = frozenset({"queue", "dispatches"})
FILTERS = frozenset({"cursor", "target_id", "state", "from", "to", "limit"})


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    object_id: str = Field(pattern=OBJECT_ID)


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    plan_id: UUID
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: StrictBool
    reason: str = Field(min_length=3, max_length=512)


class ErrorBody(BaseModel):
    code: str
    correlation_id: str


class Capabilities(BaseModel):
    environment_id: str
    server_version: str
    api_version: Literal[1]
    server_time: str
    project_timezone: str
    role: Literal["owner", "operator", "editor", "viewer"]
    actions: list[Action]


class PlanReceipt(BaseModel):
    plan_id: UUID
    plan_hash: str
    action: Action
    object_id: str
    effect: str
    expires_at: str
    details: dict


class OperationReceipt(BaseModel):
    operation_id: UUID
    action: Action
    object_id: str
    state: Literal["running", "succeeded", "blocked", "unknown"]
    code: str | None
    result: dict | None
    actor: str
    reason: str
    created_at: str
    updated_at: str


class AuditPage(BaseModel):
    items: list[OperationReceipt]
    next_cursor: str | None


def _error(status, code, correlation_id):
    headers = {"Cache-Control": "no-store", "X-Correlation-Id": correlation_id}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status == 429:
        headers["Retry-After"] = "60"
    return JSONResponse({"code": safe_code(code, "MOBILE_REQUEST_FAILED"),
                         "correlation_id": correlation_id}, status, headers=headers)


class MobileBoundary:
    """Limit request size and request rate before calling the IdP."""
    def __init__(self, app):
        self.app = app
        self.buckets = OrderedDict()
        self.lock = Lock()

    def _allow(self, key):
        current = monotonic()
        with self.lock:
            start, count = self.buckets.get(key, (current, 0))
            if current - start >= 60:
                start, count = current, 0
            if count >= (180 if key[1] == "read" else 30):
                return False
            self.buckets[key] = (start, count + 1)
            self.buckets.move_to_end(key)
            while len(self.buckets) > 1024:
                self.buckets.popitem(last=False)
            return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation = uuid4().hex
        scope.setdefault("state", {})["correlation_id"] = correlation
        write = scope["method"] == "POST"
        address = scope.get("client") or ("unknown", 0)
        if not self._allow((address[0], "write" if write else "read")):
            await _error(429, "MOBILE_RATE_LIMIT", correlation)(scope, receive, send)
            return
        if write:
            content_types = [v for k, v in scope["headers"] if k.lower() == b"content-type"]
            if (len(content_types) != 1
                    or content_types[0].partition(b";")[0].strip().lower() != b"application/json"):
                await _error(415, "MOBILE_JSON_REQUIRED", correlation)(scope, receive, send)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return
                part = message.get("body", b"")
                if len(body) + len(part) > 16384:
                    await _error(413, "MOBILE_REQUEST_TOO_LARGE", correlation)(scope, receive, send)
                    return
                body.extend(part)
                if not message.get("more_body"):
                    break
            original_receive = receive
            replayed = False

            async def receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

        started = False

        async def secured_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {b"cache-control", b"x-correlation-id"}]
                message["headers"] = [*headers, (b"cache-control", b"no-store"),
                                      (b"x-correlation-id", correlation.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, secured_send)
        except Exception:
            logger.error("MOBILE_REQUEST_UNAVAILABLE %s", correlation)
            if not started:
                await _error(503, "MOBILE_REQUEST_UNAVAILABLE", correlation)(scope, receive, send)


def _filters(request, allowed):
    query = request.query_params
    if any(key not in allowed or len(query.getlist(key)) != 1 or len(value) > 512
           for key, value in query.multi_items()):
        raise AgentError("MOBILE_FILTER_INVALID", 422)
    return dict(query)


def _cursor(data):
    if data is None:
        return None
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _audit_cursor(value):
    if value is None:
        return None
    try:
        when, operation_id = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if type(when) not in {int, float} or not math.isfinite(when) or when <= 0:
            raise ValueError
        return float(when), str(UUID(operation_id))
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise AgentError("MOBILE_FILTER_INVALID", 422) from None


def _project_actions(value, allowed):
    def item(entry):
        if "available_actions" not in entry:
            return entry
        return {**entry, "available_actions": [action for action in entry["available_actions"]
                                                if action in allowed]}
    value = item(value)
    if isinstance(value.get("items"), list):
        return {**value, "items": [item(row) for row in value["items"]]}
    return value


def mobile_app(db, domain, auth, *, environment, write_enabled=False):
    if not re.fullmatch(ID_PATTERN, environment):
        raise ValueError("MOBILE_ENVIRONMENT_INVALID")
    journal = MobileJournal(db, environment)
    app = FastAPI(title="MAX Postiz Android", version="1", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.add_middleware(MobileBoundary)
    router = APIRouter(prefix="/mobile/v1", responses={
        401: {"model": ErrorBody, "description": "Mobile session required"},
        403: {"model": ErrorBody, "description": "Insufficient permissions"},
        404: {"model": ErrorBody, "description": "Unknown resource"},
        409: {"model": ErrorBody, "description": "Stale or conflicting state"},
        422: {"model": ErrorBody, "description": "Invalid input"},
        429: {"model": ErrorBody, "description": "Rate limit"},
        503: {"model": ErrorBody, "description": "Service unavailable"},
    })
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(AgentError)
    async def known_error(request, error):
        return _error(error.status, error.code, request.state.correlation_id)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return _error(422, "MOBILE_INPUT_INVALID", request.state.correlation_id)

    def authenticate(request: Request, _credential: Annotated[
            HTTPAuthorizationCredentials | None, Security(bearer)]):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            raise AgentError("MOBILE_AUTH_REQUIRED", 401)
        return auth.authenticate(headers[0])

    # Annotated is evaluated at function definition, preserving the local dependency.
    Principal = Annotated[MobilePrincipal, Depends(authenticate)]

    def observed_receipt(receipt):
        if (receipt["action"] != "retire_preapproval_no_send"
                or receipt["state"] not in {"running", "unknown"}):
            return receipt
        observe = getattr(domain, "retirement_receipt", None)
        if observe is None:
            return receipt
        result = observe(receipt["object_id"], receipt["operation_id"], actor=receipt["actor"])
        if result is None:
            return receipt
        return {**receipt, "state": "succeeded", "code": None, "result": result}

    @router.get("/capabilities", response_model=Capabilities)
    def capabilities(request: Request, actor: Principal):
        _filters(request, ())
        return {"environment_id": environment, "server_version": __version__, "api_version": 1,
                "server_time": timestamp(now()), "project_timezone": "Europe/Moscow",
                "role": actor.role, "actions": sorted(actor.actions(write_enabled))}

    @router.post("/operation-plans", status_code=201, response_model=PlanReceipt)
    def create_plan(value: PlanInput, request: Request, actor: Principal):
        _filters(request, ())
        actor.authorize(value.action, write_enabled, fresh=True)
        payload = domain.plan(value.action, value.object_id)
        return journal.plan(actor.actor, value.action, value.object_id, payload)

    @router.post("/operations", status_code=202, response_model=OperationReceipt)
    def apply(value: OperationInput, request: Request, actor: Principal):
        _filters(request, ())
        reason = value.reason.strip()
        if (value.operation_id.version != 4 or not value.confirmed or len(reason) < 3
                or any(ord(char) < 32 for char in reason)):
            raise AgentError("MOBILE_CONFIRMATION_REQUIRED", 422)
        operation_id = str(value.operation_id)
        action_data, receipt = journal.claim(
            actor.actor, operation_id, str(value.plan_id), value.plan_hash, reason,
            lambda action: actor.authorize(action, write_enabled, fresh=True))
        if action_data is None:
            return observed_receipt(receipt)
        action, object_id, plan = action_data
        try:
            result = domain.execute(action, object_id, plan, actor=actor.actor,
                                    operation_id=operation_id, reason=reason)
        except AgentError as exc:
            code = safe_code(exc.code)
            uncertain = exc.status >= 500 or any(word in code for word in ("UNKNOWN", "UNCERTAIN"))
            return journal.finish(operation_id, "unknown" if uncertain else "blocked", code=code)
        except Exception:
            logger.error("MOBILE_OPERATION_UNCERTAIN %s", operation_id)
            return journal.finish(operation_id, "unknown", code="MOBILE_OPERATION_UNCERTAIN")
        return journal.finish(operation_id, "succeeded", result=result)

    @router.get(
        "/operations/{operation_id}",
        response_model=OperationReceipt,
        responses={410: {
            "model": ErrorBody,
            "description": "MOBILE_OPERATION_NOT_ACCEPTED for an exact expired unused plan of the authenticated actor and environment",
        }},
        openapi_extra={"parameters": [
            {
                "name": "plan_id", "in": "query", "required": False,
                "description": "Use with plan_hash after a missing operation receipt to check the expired original plan.",
                "schema": {"type": "string", "format": "uuid"},
            },
            {
                "name": "plan_hash", "in": "query", "required": False,
                "description": "Exact hash returned when the original plan was created.",
                "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        ]},
    )
    def operation_status(operation_id: UUID, request: Request, actor: Principal):
        query = _filters(request, {"plan_id", "plan_hash"})
        if not query:
            return observed_receipt(journal.status(actor.actor, str(operation_id)))
        try:
            plan_id = UUID(query.get("plan_id", ""))
        except ValueError:
            raise AgentError("MOBILE_FILTER_INVALID", 422) from None
        plan_hash = query.get("plan_hash", "")
        if plan_id.version != 4 or operation_id.version != 4 or not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
            raise AgentError("MOBILE_FILTER_INVALID", 422)
        return observed_receipt(journal.status(actor.actor, str(operation_id),
                                               plan_id=str(plan_id), plan_hash=plan_hash))

    @router.get("/audit", response_model=AuditPage)
    def audit(request: Request, actor: Principal):
        query = _filters(request, {"cursor", "limit"})
        limit = query.get("limit", "25")
        if not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= 50:
            raise AgentError("MOBILE_FILTER_INVALID", 422)
        rows, cursor = journal.audit(None if actor.role == "owner" else actor.actor,
                                     _audit_cursor(query.get("cursor")), int(limit))
        return {"items": [observed_receipt(row) for row in rows], "next_cursor": _cursor(cursor)}

    @router.get("/{resource}")
    def read(resource: str, request: Request, actor: Principal):
        if resource not in READS:
            raise AgentError("MOBILE_RESOURCE_NOT_FOUND", 404)
        query = _filters(request, FILTERS if resource in {"calendar", "queue"} else
                         {"cursor", "limit"} if resource == "targets" else ())
        value = domain.read(resource, filters=query)
        return _project_actions(value, actor.actions(write_enabled))

    @router.get("/{resource}/{object_id}")
    def details(resource: str, object_id: str, request: Request, actor: Principal):
        if resource not in DETAILS or not re.fullmatch(OBJECT_ID, object_id):
            raise AgentError("MOBILE_RESOURCE_NOT_FOUND", 404)
        _filters(request, ())
        value = domain.read(resource, object_id=object_id, filters={})
        return _project_actions(value, actor.actions(write_enabled))

    app.include_router(router)
    return app
