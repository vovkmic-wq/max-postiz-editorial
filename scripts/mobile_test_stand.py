"""Изолированный стенд M1 с синтетическими платформами и локальной PostgreSQL."""
import argparse
import hmac
import json
import os
import secrets
import shutil
from pathlib import Path
from urllib.parse import quote, urlsplit

import uvicorn
from sqlalchemy import select

from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.db import Database, Draft, now
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.mobile_api import mobile_app
from max_postiz_agent.mobile_auth import MobilePrincipal
from max_postiz_agent.mobile_domain import MobileDomain
from max_postiz_agent.server_state import ServerState

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (
    ("max_fixture", "max", "max_browser", "Демонстрационный MAX"),
    ("telegram_fixture", "telegram", "telegram_api", "Сверка Telegram"),
    ("vk_fixture", "vk", "vk_browser", "Очередь VK"),
    ("pikabu_fixture", "pikabu", "pikabu_browser", "Черновик Пикабу"),
)


class FixturePostiz:
    def posts(self, *_args, **_kwargs):
        return []

    def close(self):
        pass


class FixturePublisher:
    def call(self, *_args, **_kwargs):
        raise AgentError("MOBILE_FIXTURE_TRANSPORT_DISABLED", 409)

    def close(self):
        pass


class FixtureAuth:
    def __init__(self, token):
        self.expected = "Bearer " + token

    def authenticate(self, value):
        if not hmac.compare_digest(value, self.expected):
            raise AgentError("MOBILE_AUTH_REQUIRED", 401)
        return MobilePrincipal("postiz:mobile:" + "1" * 32, "owner", now())


def seed(core):
    ServerState(core).initialize()
    with core.db.session.begin() as session:
        existing = set(session.scalars(select(Target.id)))
        if existing:
            if existing != {route[0] for route in ROUTES}:
                raise RuntimeError("Use a dedicated editorial_mobile_fixture database")
            return
        for target_id, platform, route, title in ROUTES:
            binding = fingerprint([target_id, route])
            target = Target(id=target_id, profile_id="main", platform=platform, route=route,
                            destination=target_id, enabled=True, paused=False,
                            binding_hash=binding, options={})
            draft = Draft(channel_id="main", story_id=target_id, current={},
                          config_id=core.settings.hash)
            session.add_all((target, draft))
            session.flush()
            variant = Variant(id=target_id, draft_id=draft.id, target_id=target.id,
                              title=title, body=title, source_revision=1,
                              config_hash=core.settings.hash, binding_hash=binding,
                              metadata_json={"origin": "manual_original"})
            session.add(variant)
            session.flush()
            if platform in {"telegram", "vk"}:
                due_at = now() - 60 if platform == "telegram" else now() + 1800
                snapshot = core.snapshot(variant, target)
                digest = fingerprint({"content": snapshot, "due_at": due_at})
                approval = TargetApproval(variant_id=variant.id, revision=variant.revision,
                                          snapshot_hash=digest, scheduled_at=due_at,
                                          actor="stand-fixture")
                session.add(approval)
                session.flush()
                session.add(Dispatch(id=target_id, variant_id=variant.id, approval_id=approval.id,
                                     revision=variant.revision, due_at=due_at, snapshot=snapshot,
                                     snapshot_hash=digest, state="unknown" if platform == "telegram"
                                     else "pending", attempts=int(platform == "telegram")))
                variant.state = "unknown" if platform == "telegram" else "approved"


def fixture_reconcile(dispatch, target):
    if (dispatch.id, target.id) != ("telegram_fixture", "telegram_fixture"):
        return Delivery("unknown", evidence={"fixture": True, "failure_code": "MOBILE_FIXTURE_NO_PROOF"})
    return Delivery("confirmed", "stand-telegram", verified=True, evidence={"fixture": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18093)
    parser.add_argument("--container-bind", action="store_true",
                        help="Запуск в отдельной Docker-сети с fixture-db")
    args = parser.parse_args()
    host = "fixture-db" if args.container_bind else "127.0.0.1"
    if args.container_bind:
        if getattr(os, "geteuid", lambda: 0)() == 0:
            parser.error("Use an unprivileged Linux user in the Docker fixture")
        try:
            password = Path("/run/secrets/mobile_fixture_pg_password").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            parser.error("The Docker fixture database secret is required")
        if not password:
            parser.error("The Docker fixture database secret is empty")
        url = f"postgresql+psycopg://mobile_test:{quote(password, safe='')}@{host}:5432/editorial_mobile_fixture"
    else:
        url = os.environ.get("MOBILE_FIXTURE_DATABASE_URL", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "postgresql+psycopg" or parsed.hostname != host
            or parsed.path not in {"/editorial_mobile_fixture", "/editorial_mobile_fixture_ui"}
            or parsed.username != "mobile_test"
            or parsed.query or parsed.fragment):
        parser.error("Use the dedicated loopback fixture database")
    path = args.runtime.resolve()
    path.mkdir(parents=True, exist_ok=False)
    shutil.copytree(ROOT / "schemas", path / "schemas")
    shutil.copytree(ROOT / "prompts", path / "prompts")
    shutil.copytree(ROOT / "migrations", path / "migrations", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "alembic.ini", path / "alembic.ini")
    shutil.copy2(ROOT / "TOPIC.md", path / "TOPIC.md")
    (path / "config").mkdir()
    shutil.copy2(ROOT / "config/app.example.json", path / "config/app.json")
    shutil.copy2(ROOT / "config/sources.example.json", path / "config/sources.json")
    settings = Settings.load(path)
    db = Database(settings, url=url)
    db.initialize()
    core = HubService(settings, db, publisher=FixturePublisher(), postiz=FixturePostiz(),
                      bootstrap_targets=False)
    try:
        seed(core)
        core.routes.reconcile = fixture_reconcile
        token = secrets.token_urlsafe(32)
        (path / "fixture-access.private.json").write_text(
            json.dumps({"token": token, "port": args.port}), encoding="utf-8")
        app = mobile_app(db, MobileDomain(core), FixtureAuth(token),
                         environment="android-fixture", write_enabled=True)
        uvicorn.run(app, host="0.0.0.0" if args.container_bind else "127.0.0.1",  # noqa: S104
                    port=args.port, proxy_headers=False, access_log=False)
    finally:
        core.close()


if __name__ == "__main__":
    main()
