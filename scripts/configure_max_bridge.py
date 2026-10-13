"""Enable the local bridge and connect the allowed MAX account through Postiz's own API."""
import argparse
import json
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_cli import build


def settings(root):
    env = root / ".env.local"
    lines = env.read_text(encoding="utf-8-sig").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    key = values.get("MAX_POSTIZ_BRIDGE_KEY") or secrets.token_urlsafe(48)
    lines = [line for line in lines if not line.startswith("MAX_POSTIZ_BRIDGE_KEY=")]
    env.write_text("\n".join([*lines, "MAX_POSTIZ_BRIDGE_KEY=" + key]) + "\n", encoding="utf-8")
    deployment = root / "deployment/postiz/.env"
    lines = [line for line in deployment.read_text(encoding="utf-8").splitlines()
             if not line.startswith("MAX_POSTIZ_BRIDGE_KEY=")]
    deployment.write_text("\n".join([*lines, "MAX_POSTIZ_BRIDGE_KEY=" + key]) + "\n", encoding="utf-8")
    path = root / "config/publishing.json"
    publishing = json.loads(path.read_text(encoding="utf-8-sig"))
    publishing["postiz"]["browser_bridge_enabled"] = True
    path.write_text(json.dumps(publishing, ensure_ascii=False, indent=2), encoding="utf-8")


def connect(root):
    load_env(root)
    access = json.loads((root / "data/postiz-access.json").read_text(encoding="utf-8"))
    core = build(root)
    try:
        integrations = [i for i in core.postiz.integrations() if i["identifier"] == "max-browser"]
        if not integrations:
            with httpx.Client(base_url=access["base_url"], timeout=90, trust_env=False) as client:
                r = client.post("/api/auth/login", json={"email": access["email"], "password": access["password"], "provider": "LOCAL"})
                if r.status_code != 200 or not r.headers.get("auth"):
                    raise AgentError("POSTIZ_LOGIN_FAILED")
                client.cookies.set("auth", r.headers["auth"], domain="localhost.local", path="/")
                r = client.get("/api/integrations/social/max-browser")
                if r.status_code != 200 or not r.json().get("url"):
                    raise AgentError("MAX_BRIDGE_CONNECT_URL_FAILED")
                parsed = urlsplit(r.json()["url"])
                if (parsed.scheme, parsed.netloc) != ("http", "localhost:4007"):
                    raise AgentError("MAX_BRIDGE_CONNECT_ORIGIN_INVALID")
                query = parse_qs(parsed.query)
                r = client.post("/api/integrations/social-connect/max-browser",
                                json={"state": query["state"][0], "code": query["code"][0], "timezone": "-180"})
                if r.status_code not in {200, 201} or r.json().get("error"):
                    raise AgentError("MAX_BRIDGE_CONNECT_FAILED")
            integrations = [i for i in core.postiz.integrations() if i["identifier"] == "max-browser"]
        if len(integrations) != 1:
            raise AgentError("MAX_BRIDGE_INTEGRATION_AMBIGUOUS")
        path = root / "config/publishing.json"
        publishing = json.loads(path.read_text(encoding="utf-8"))
        target = next(t for t in publishing["targets"] if t["id"] == "max_test")
        if target["route"] != "max_browser" or target["destination"] != "-79860712657053":
            raise AgentError("MAX_BRIDGE_TARGET_CHANGED")
        target["options"]["postiz_integration_id"] = integrations[0]["id"]
        path.write_text(json.dumps(publishing, ensure_ascii=False, indent=2), encoding="utf-8")
        core.activate()
        core.bridge.sync(force=True)
        report = {"state": "connected", "provider": "max-browser", "integration_id": integrations[0]["id"],
                  "target_id": target["id"], "destination": target["destination"], "delivery_paused": True,
                  "new_external_posts": 0}
        (root / "data/postiz-max-connection.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        core.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "enable-image", "connect"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.action == "prepare":
        settings(root)
        print("Private bridge settings prepared; existing publication pauses retained")
    elif args.action == "enable-image":
        (root / "data/postiz-max-bridge-enabled").write_text("0.4.0\n", encoding="utf-8")
        print("MAX browser image overlay selected")
    else:
        connect(root)


if __name__ == "__main__":
    try:
        main()
    except (AgentError, httpx.HTTPError, ValueError, KeyError, StopIteration) as exc:
        print(json.dumps({"state": "failed", "code": exc.code if isinstance(exc, AgentError) else type(exc).__name__}))
        raise SystemExit(1) from None
