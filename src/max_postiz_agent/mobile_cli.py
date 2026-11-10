"""Шлюз за частным HTTPS-прокси с отдельной привязкой внутри Docker."""
import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from .cli import load_env
from .config import AgentError
from .hub_cli import build
from .mobile_api import mobile_app
from .mobile_auth import MobileOIDCAuth, MobileOIDCConfig
from .mobile_domain import MobileDomain


def main(argv=None):
    parser = argparse.ArgumentParser(description="Мобильный шлюз MAX Postiz")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--container-bind", action="store_true",
                        help="Слушать внутри Docker. Публикуйте порт только на loopback хоста")
    parser.add_argument("--print-openapi", action="store_true")
    args = parser.parse_args(argv)
    if args.print_openapi:
        print(json.dumps(mobile_app(None, None, None, environment="schema").openapi(),
                         ensure_ascii=True, indent=2))
        return
    if not 1 <= args.port <= 65535:
        parser.error("Неверный порт")
    if args.container_bind and getattr(os, "geteuid", lambda: 0)() == 0:
        parser.error("Слушатель контейнера требует непривилегированного пользователя Linux")
    root = args.root.resolve()
    load_env(root)
    core = auth = None
    try:
        config = MobileOIDCConfig.from_env()
        environment = os.getenv("MOBILE_ENVIRONMENT_ID", "")
        auth = MobileOIDCAuth(config)
        core = build(root, activate=False, initialize=False)
        if core.db.engine.dialect.name != "postgresql":
            raise AgentError("MOBILE_POSTGRESQL_REQUIRED", 503)
        unavailable_browser_routes = set()
        if sys.platform == "linux":
            from .browser_process import linux_runtime_status
            status = linux_runtime_status()
            if not all(status[key] for key in ("supported", "writable", "cgroup_kill")):
                unavailable_browser_routes = {"vk_browser", "pikabu_browser"}
        domain = MobileDomain(core, unavailable_browser_routes=unavailable_browser_routes)
        domain.store.read()
        app = mobile_app(core.db, domain, auth, environment=environment,
                         write_enabled=os.getenv("MOBILE_WRITE_ENABLED") == "true")
        uvicorn.run(app, host="0.0.0.0" if args.container_bind else "127.0.0.1",  # noqa: S104
                    port=args.port, proxy_headers=False,
                    access_log=False, log_level="warning", limit_concurrency=64)
    except (ValueError, AgentError) as error:
        code = error.code if isinstance(error, AgentError) else "MOBILE_CONFIG_INVALID"
        parser.exit(1, code + "\n")
    finally:
        if core is not None:
            core.close()
        if auth is not None:
            auth.client.close()


if __name__ == "__main__":
    main()
