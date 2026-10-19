"""Documented Postiz public API, with no implicit retry of mutating requests."""
import os
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from .config import AgentError


class PostizClient:
    def __init__(self, options, transport=None):
        self.options = options
        self.base = os.getenv(options.get("base_url_env", "POSTIZ_BASE_URL"), "").rstrip("/")
        self.key = os.getenv(options.get("api_key_env", "POSTIZ_API_KEY"), "")
        self.public_origin = os.getenv(options.get('public_origin_env', 'POSTIZ_PUBLIC_ORIGIN'), '').rstrip('/')
        if self.public_origin:
            public = urlsplit(self.public_origin)
            if (public.scheme not in {'http', 'https'} or not public.netloc or public.path
                    or public.username or public.password or public.query or public.fragment):
                raise AgentError('POSTIZ_PUBLIC_ORIGIN_INVALID')
        self.client = httpx.Client(timeout=options.get("timeout_seconds", 30),
                                   trust_env=False, follow_redirects=False, transport=transport)
        self.max_video = options.get("max_video_verified", False)

    def request(self, method, path, **kwargs):
        parsed = urlsplit(self.base)
        if not self.key or not self.base or parsed.scheme not in {"http", "https"}:
            raise AgentError("POSTIZ_NOT_CONFIGURED", 503)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AgentError("POSTIZ_BASE_URL_INVALID")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "postiz"}:
            raise AgentError("POSTIZ_HTTPS_REQUIRED")
        try:
            response = self.client.request(method, self.base + path,
                                           headers={"Authorization": self.key}, **kwargs)
        except httpx.HTTPError as exc:
            raise AgentError("POSTIZ_UNKNOWN" if method != "GET" else "POSTIZ_UNAVAILABLE", 503) from exc
        if response.status_code in {401, 403}:
            raise AgentError("POSTIZ_ACCESS_DENIED", 403)
        if response.status_code == 429:
            raise AgentError("POSTIZ_RATE_LIMIT", 429)
        if response.status_code >= 500 or response.is_redirect:
            raise AgentError("POSTIZ_UNKNOWN" if method != "GET" else "POSTIZ_UNAVAILABLE", 503)
        if response.status_code >= 400:
            raise AgentError("POSTIZ_REJECTED")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AgentError("POSTIZ_UNKNOWN" if method != "GET" else "POSTIZ_RESPONSE_INVALID", 503) from exc

    def integrations(self):
        data = self.request("GET", "/integrations")
        if not isinstance(data, list):
            raise AgentError("POSTIZ_RESPONSE_INVALID")
        return data

    def posts(self, start=None, end=None):
        start = start or datetime(2000, 1, 1, tzinfo=UTC)
        end = end or datetime(2100, 1, 1, tzinfo=UTC)
        data = self.request("GET", "/posts", params={"startDate": start.isoformat(),
                                                     "endDate": end.isoformat()})
        if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
            raise AgentError("POSTIZ_RESPONSE_INVALID")
        return data["posts"]

    def get_post(self, post_id):
        found = [p for p in self.posts() if p.get("id") == post_id]
        if len(found) != 1:
            raise AgentError("POSTIZ_POST_NOT_FOUND", 409)
        return found[0]

    def upload(self, path, mime):
        with path.open("rb") as stream:
            data = self.request("POST", "/upload", files={"file": (path.name, stream, mime)})
        if not isinstance(data, dict) or not data.get("id") or not data.get("path"):
            raise AgentError("POSTIZ_RESPONSE_INVALID")
        return {"id": data["id"], "path": data["path"]}

    def create(self, target, content, media, kind="draft", due_at=None, tag=None):
        if kind not in {"draft", "schedule"}:
            raise AgentError("POSTIZ_TYPE_INVALID")
        settings = dict(target.options.get("settings", {}))
        settings["__type"] = target.platform
        date = datetime.now(UTC) if due_at is None else datetime.fromtimestamp(due_at, UTC)
        body = {"type": kind, "date": date.isoformat(),
                "shortLink": False, "tags": [], "posts": [{"integration": {"id": target.destination},
                "value": [{"content": content, "image": media}], "settings": settings}]}
        # Correlation is local metadata, not a visible watermark or unsupported API field.
        data = self.request("POST", "/posts", json=body)
        if not isinstance(data, list) or len(data) != 1 or not data[0].get("postId"):
            raise AgentError("POSTIZ_UNKNOWN", 503)
        return data[0]["postId"]

    def delete(self, post_id):
        if not isinstance(post_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", post_id):
            raise AgentError("POSTIZ_ID_INVALID")
        return self.request("DELETE", "/posts/" + quote(post_id, safe=""))

    def reschedule_existing(self, post_id, expected_body, due_at):
        """Update one unpublished text post through createOrUpdatePost by its ID."""
        from .max_bridge import MaxBridge
        import json
        remote = self.get_post(post_id)
        body, images, _, _ = MaxBridge.normalize(remote)
        if remote["state"] not in {"DRAFT", "QUEUE"} or images or body != expected_body:
            raise AgentError("BRIDGE_REMOTE_CHANGED", 409)
        settings = remote.get("settings") or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        if not isinstance(settings, dict) or settings.get("__type") != "max-browser":
            raise AgentError("BRIDGE_REMOTE_INVALID", 409)
        payload = {"type": "schedule", "date": datetime.fromtimestamp(due_at, UTC).isoformat(),
                   "shortLink": False, "tags": [], "republish": False,
                   "posts": [{"integration": {"id": remote["integration"]["id"]}, "group": remote["group"],
                              "settings": settings, "value": [{"id": post_id,
                              "content": remote["content"], "image": []}]}]}
        result = self.request("POST", "/posts", json=payload)
        if (not isinstance(result, list) or len(result) != 1
                or not isinstance(result[0], dict) or result[0].get("postId") != post_id):
            raise AgentError("POSTIZ_UNKNOWN", 503)
        return post_id

    def close(self):
        self.client.close()
