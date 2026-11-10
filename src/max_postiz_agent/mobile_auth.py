"""Production mobile authentication through a configured OIDC introspection endpoint."""
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import AgentError
from .db import now

OWNER_ACTIONS = frozenset({"resume_project", "resume_target", "retire_preapproval_no_send"})
OPERATOR_ACTIONS = frozenset({
    "pause_project", "pause_target", "hold_item", "release_item_hold", "reconcile_dispatch",
})
ROLES = frozenset({"viewer", "editor", "operator", "owner"})
ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"


@dataclass(frozen=True)
class MobilePrincipal:
    actor: str
    role: str
    auth_time: float | None

    def actions(self, write_enabled):
        if not write_enabled:
            return frozenset()
        if self.role == "owner":
            return OPERATOR_ACTIONS | OWNER_ACTIONS
        if self.role == "operator":
            return OPERATOR_ACTIONS
        return frozenset()

    def authorize(self, action, write_enabled, *, fresh=False):
        if action not in self.actions(write_enabled):
            raise AgentError("MOBILE_ACTION_FORBIDDEN", 403)
        if fresh and action in OWNER_ACTIONS:
            if self.auth_time is None or not -30 <= now() - self.auth_time <= 300:
                raise AgentError("MOBILE_REAUTH_REQUIRED", 403)


def _private_https(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("MOBILE_AUTH_CONFIG_INVALID")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.query):
        raise ValueError("MOBILE_AUTH_CONFIG_INVALID")
    return parsed


@dataclass(frozen=True, repr=False)
class MobileOIDCConfig:
    issuer: str
    endpoint: str
    introspection_id: str
    introspection_secret: str
    android_client_id: str
    audience: str
    subjects: dict[str, str]

    def __post_init__(self):
        issuer, endpoint = _private_https(self.issuer), _private_https(self.endpoint)
        if (issuer.hostname, issuer.port or 443) != (endpoint.hostname, endpoint.port or 443):
            raise ValueError("MOBILE_AUTH_CONFIG_INVALID")
        credentials = (self.introspection_id, self.introspection_secret,
                       self.android_client_id, self.audience)
        if any(not isinstance(value, str) or not 1 <= len(value) <= 1024 for value in credentials):
            raise ValueError("MOBILE_AUTH_CONFIG_INVALID")
        if (not isinstance(self.subjects, dict) or not 1 <= len(self.subjects) <= 64
                or any(not isinstance(key, str) or not 1 <= len(key) <= 255
                       or not isinstance(role, str) or role not in ROLES
                       for key, role in self.subjects.items())
                or sum(role == "owner" for role in self.subjects.values()) != 1):
            raise ValueError("MOBILE_AUTH_CONFIG_INVALID")

    @classmethod
    def from_env(cls):
        try:
            return cls(os.environ["MOBILE_OIDC_ISSUER"], os.environ["MOBILE_OIDC_INTROSPECTION_URL"],
                       os.environ["MOBILE_OIDC_INTROSPECTION_CLIENT_ID"],
                       os.environ["MOBILE_OIDC_INTROSPECTION_SECRET"],
                       os.environ["MOBILE_OIDC_ANDROID_CLIENT_ID"], os.environ["MOBILE_OIDC_AUDIENCE"],
                       json.loads(os.environ["MOBILE_OIDC_SUBJECT_ROLES"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("MOBILE_AUTH_CONFIG_INVALID") from None


def _time(value):
    return float(value) if type(value) in {float, int} and 0 < value < 32_503_680_000 else None


class MobileOIDCAuth:
    def __init__(self, config, *, client=None):
        self.config = config
        self.client = client or httpx.Client(
            timeout=5, follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=4))

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not re.fullmatch(
                r"Bearer [A-Za-z0-9._~+/\-=]{1,8192}", authorization, re.IGNORECASE):
            raise AgentError("MOBILE_AUTH_REQUIRED", 401)
        token = authorization.split(" ", 1)[1]
        config = self.config
        try:
            with self.client.stream(
                "POST", config.endpoint,
                auth=(config.introspection_id, config.introspection_secret),
                data={"token": token, "token_type_hint": "access_token"},
                headers={"Accept": "application/json"},
            ) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > 16384:
                        raise ValueError
                    data.extend(chunk)
                claims = json.loads(data)
        except (httpx.HTTPError, ValueError):
            raise AgentError("MOBILE_AUTH_UNAVAILABLE", 503) from None
        if not isinstance(claims, dict):
            raise AgentError("MOBILE_AUTH_UNAVAILABLE", 503)
        if claims.get("active") is not True:
            raise AgentError("MOBILE_SESSION_EXPIRED", 401)
        audiences = claims.get("aud")
        audiences = [audiences] if isinstance(audiences, str) else audiences
        expiration = _time(claims.get("exp"))
        subject = claims.get("sub")
        if (claims.get("iss") != config.issuer
                or claims.get("client_id") != config.android_client_id
                or not isinstance(audiences, list) or config.audience not in audiences
                or expiration is None or not math.isfinite(expiration) or expiration <= now()
                or not isinstance(subject, str) or not 1 <= len(subject) <= 255):
            raise AgentError("MOBILE_SESSION_INVALID", 401)
        role = config.subjects.get(subject)
        if role is None:
            raise AgentError("MOBILE_SUBJECT_FORBIDDEN", 403)
        actor_hash = hashlib.sha256(f"{config.issuer}\0{subject}".encode()).hexdigest()[:32]
        return MobilePrincipal(f"postiz:mobile:{actor_hash}", role, _time(claims.get("auth_time")))
