"""Create/reuse the local Postiz owner and connect its API without printing secrets."""
import json
import secrets
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    access_path = root / "data/postiz-access.json"
    if access_path.exists():
        access = json.loads(access_path.read_text(encoding="utf-8"))
    else:
        access = {"base_url": "http://localhost:4007", "email": "admin@editorial.local",
                  "password": secrets.token_urlsafe(32)}
        access_path.parent.mkdir(parents=True, exist_ok=True)
        access_path.write_text(json.dumps(access, indent=2), encoding="utf-8")
    base = access["base_url"]
    payload = {"email": access["email"], "password": access["password"], "provider": "LOCAL"}
    with httpx.Client(timeout=30, trust_env=False, base_url=base) as client:
        response = client.post("/api/auth/login", json=payload)
        if response.status_code == 400:
            allowed = client.get("/api/auth/can-register")
            if allowed.status_code != 200 or not allowed.json().get("register"):
                raise RuntimeError("POSTIZ_OWNER_LOGIN_FAILED")
            response = client.post("/api/auth/register", json={**payload, "company": "Editorial Hub"})
        if response.status_code != 200:
            raise RuntimeError("POSTIZ_OWNER_BOOTSTRAP_FAILED")
        # Local HTTP authentication can also expose the cookie as a header.
        if response.headers.get("auth"):
            client.cookies.set("auth", response.headers["auth"], domain="localhost.local", path="/")
        info = client.get("/api/user/self")
        if info.status_code != 200:
            raise RuntimeError("POSTIZ_OWNER_SESSION_FAILED")
        key = info.json().get("publicApi")
        if not isinstance(key, str) or not key:
            raise RuntimeError("POSTIZ_API_KEY_MISSING")
        api_base = base + "/api/public/v1"
        integrations = client.get(api_base + "/integrations", headers={"Authorization": key})
        if integrations.status_code != 200 or not isinstance(integrations.json(), list):
            raise RuntimeError("POSTIZ_PUBLIC_API_FAILED")
        access["api_key"] = key
        access_path.write_text(json.dumps(access, indent=2), encoding="utf-8")
        env_path = root / ".env.local"
        lines = env_path.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if not line.startswith(("POSTIZ_BASE_URL=", "POSTIZ_API_KEY="))]
        lines += ["POSTIZ_BASE_URL=" + api_base, "POSTIZ_API_KEY=" + key]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps({"state": "connected", "base_url": base,
                          "public_api_verified": True, "integrations": len(integrations.json()),
                          "access_file": "data/postiz-access.json"}))


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
        # Response bodies, URLs, cookies, passwords and API keys must never enter output.
        print(json.dumps({"state": "failed", "code": str(exc) if isinstance(exc, RuntimeError)
                          else type(exc).__name__}))
        raise SystemExit(1) from None
