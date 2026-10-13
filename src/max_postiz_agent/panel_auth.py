"""Вход панели через подписанную HttpOnly-сессию; Bearer API остаётся доступным."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from .config import AgentError

COOKIE = "max_panel_session"
LIFETIME = 8 * 60 * 60


# Роль сессии проверяется её собственным ключом; замена ключа отзывает старые сессии.
def role_keys(settings):
    return {"owner": os.getenv(settings.app["api"]["auth_secret_env"], ""),
            "editor": os.getenv("APP_EDITOR_SECRET", ""),
            "viewer": os.getenv("APP_VIEWER_SECRET", "")}


# Неподписанное содержимое сессии не даёт никаких прав.
def encode_session(settings, role, *, at=None):
    current = int(time.time() if at is None else at)
    key = role_keys(settings).get(role)
    if not key:
        raise AgentError("AUTH_INVALID", 401)
    payload = base64.urlsafe_b64encode(json.dumps({
        "role": role, "expires": current + LIFETIME, "nonce": secrets.token_urlsafe(16),
    }, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


# Проверяем подпись, срок и роль без обращения к базе или раскрытия ключа браузеру.
def decode_session(settings, token, *, at=None):
    try:
        payload, signature = token.split(".")
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        key = role_keys(settings).get(value["role"])
        if not key or len(token) > 1024:
            raise ValueError
        expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
        if value["expires"] <= (time.time() if at is None else at):
            raise AgentError("SESSION_EXPIRED", 401)
        return value["role"]
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise AgentError("AUTH_INVALID", 401) from exc


# Cookie-запросы, меняющие состояние, допускаются только из самой панели.
def authenticate(settings, request, authorization=None):
    if authorization:
        if not authorization.startswith("Bearer "):
            raise AgentError("AUTH_REQUIRED", 401)
        supplied = authorization[7:]
        for role, key in role_keys(settings).items():
            if key and hmac.compare_digest(key.encode(), supplied.encode()):
                return role
        raise AgentError("AUTH_INVALID", 401)
    token = request.cookies.get(COOKIE)
    if not token:
        raise AgentError("AUTH_REQUIRED", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin", "").rstrip("/")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin != expected:
            raise AgentError("AUTH_ORIGIN_FORBIDDEN", 403)
    return decode_session(settings, token)
