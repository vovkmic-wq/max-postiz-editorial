"""Запуск автономной редакции на VM без старой панели Hub."""
import argparse
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from .article_cycle import HourlyArticleCycle
from .config import AgentError
from .hub_cli import build
from .server_api import editorial_app, public_state
from .server_state import DatabaseArticleLedger, ServerState


def main(argv=None):
    parser = argparse.ArgumentParser(description="Автономная редакция Postiz")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Сохранить журнал в PostgreSQL; запуск остаётся на паузе")
    sub.add_parser("status")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8092)
    serve.add_argument("--api-only", action="store_true",
                       help="Проверка внутреннего API без второго обработчика")
    args = parser.parse_args(argv)
    core = build(args.root.resolve())
    store = ServerState(core)
    try:
        if args.command == "migrate":
            cycle = HourlyArticleCycle(core)
            cycle._clear_outstanding(cycle._policy(enabled=False)["target_id"])
            result = DatabaseArticleLedger(cycle).migrate()
            store.initialize()
            print(json.dumps({"state": "migrated_paused", **result}, ensure_ascii=False))
        elif args.command == "status":
            print(json.dumps(public_state(core, store), ensure_ascii=False))
        else:
            import uvicorn
            from .server_cycle import ServerCycle
            store.read()
            if DatabaseArticleLedger(HourlyArticleCycle(core)).load() is None:
                raise AgentError("EDITORIAL_MIGRATION_REQUIRED", 409)
            if args.api_only:
                uvicorn.run(editorial_app(core, store), host=args.host, port=args.port,
                            access_log=False, log_level="warning")
                return
            with store.worker_lock() as acquired:
                if not acquired:
                    raise AgentError("EDITORIAL_WORKER_ALREADY_RUNNING", 409)
                app = editorial_app(core, store)
                app.state.worker_mode = "autonomous"
                stop = threading.Event()
                controller = ServerCycle(core, state=store)

                def worker():
                    # В процессе ровно один обработчик: сначала доставка, затем подготовка.
                    while not stop.is_set():
                        try:
                            core.dispatch_tick()
                            controller.run_once()
                        except Exception as exc:
                            code = exc.code if isinstance(exc, AgentError) else "EDITORIAL_WORKER_ERROR"
                            try:
                                store.phase("blocked", last_error={"code": code})
                            except Exception:
                                # Сбой БД не должен незаметно убить поток при живом HTTP-процессе.
                                logging.getLogger(__name__).error("EDITORIAL_STATE_UNAVAILABLE")
                        stop.wait(5)

                @asynccontextmanager
                async def lifespan(application):
                    thread = threading.Thread(target=worker, name="editorial-cycle", daemon=True)
                    thread.start()
                    try:
                        yield
                    finally:
                        stop.set()
                        # Принудительный обрыв не вызывает повтор неизвестной операции.
                        thread.join(timeout=10)

                app.router.lifespan_context = lifespan
                uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")
    finally:
        core.close()


if __name__ == "__main__":
    main()
