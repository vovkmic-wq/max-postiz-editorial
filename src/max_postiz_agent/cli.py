# Разбор параметров командной строки и запуск сервисов проекта.
"""Cross-platform commands. No implicit external posting during setup or demo."""

import argparse
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from .config import AgentError, Settings
from .db import Database
from .service import Service


# Загружаем локальные переменные без вывода значений ключей.
def load_env(root):
    path = root / ".env.local"
    # Обрабатываем ветвь при условии: path.exists().
    if path.exists():
        # Последовательно обрабатываем элементы: path.read_text(encoding='utf-8-sig').splitlines().
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            # Обрабатываем ветвь при условии: line.strip() and (not line.lstrip().startswith('#')) and ('=' in line).
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


# Создаём отсутствующие настройки или структуру хранения.
def initialize(root):
    # Последовательно обрабатываем элементы: ('app', 'sources', 'image_sources').
    for name in ("app", "sources", "image_sources", "holidays"):
        if not (root / f"config/{name}.example.json").exists():
            continue
        target = root / f"config/{name}.json"
        # Обрабатываем ветвь при условии: not target.exists().
        if not target.exists():
            shutil.copy2(root / f"config/{name}.example.json", target)
    env = root / ".env.local"
    # Обрабатываем ветвь при условии: not env.exists().
    if not env.exists():
        env.write_text(
            "# Local panel secrets. Never copy into prompts or release archives.\n"
            + "\n".join(
                f"{key}={secrets.token_urlsafe(32)}"
                for key in (
                    "APP_AUTH_SECRET",
                    "APP_EDITOR_SECRET",
                    "APP_VIEWER_SECRET",
                    "MAX_WEBHOOK_SECRET",
                    "TELEGRAM_WEBHOOK_SECRET",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        # Обрабатываем ветвь при условии: os.name != 'nt'.
        if os.name != "nt":
            env.chmod(0o600)
    load_env(root)


# Открываем базу и собираем сервис из проверенной конфигурации.
def build_service(root):
    load_env(root)
    settings = Settings.load(root)
    db = Database(settings)
    db.initialize()
    service = Service(settings, db)
    service.activate()
    return service


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main(argv=None):
    # Обрабатываем ветвь при условии: hasattr(sys.stdout, 'reconfigure').
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Агент каналов MAX")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    # Последовательно обрабатываем элементы: ('init', 'doctor', 'tick', 'worker').
    for command in ("init", "doctor", "tick", "worker"):
        sub.add_parser(command)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    create = sub.add_parser("browser-create-channel")
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    create.add_argument("--recover-chat-id", help="Сверить уже созданный канал после неопределённого результата")
    create.add_argument("--bind", action="store_true", help="Добавить созданный канал в разрешённые адресаты")
    channels = sub.add_parser("channels", help="Профили, тематика и подключение нескольких каналов")
    channel_commands = channels.add_subparsers(dest="channel_command", required=True)
    channel_commands.add_parser("list")
    channel_commands.add_parser("apply").add_argument("--file", type=Path, required=True)
    connect = channel_commands.add_parser("connect")
    connect.add_argument("--channel", required=True)
    connect.add_argument("--chat-id", required=True)
    images = sub.add_parser("images", help="Необязательное управление подбором изображений из Codex или CLI")
    image_commands = images.add_subparsers(dest="image_command", required=True)
    image_scan = image_commands.add_parser("scan")
    image_scan.add_argument("--all-sources", action="store_true", help="Разовый обход всех источников")
    image_scan.add_argument("--unused", action="store_true", help="Исключить ранее подготовленные изображения")
    # Последовательно обрабатываем элементы: ('top', 'status').
    for name in ("top", "status"):
        image_commands.add_parser(name)
    image_publish = image_commands.add_parser("publish")
    image_publish.add_argument("--rank", type=int, choices=range(1, 11), required=True)
    image_publish.add_argument("--sha256", required=True, help="SHA-256 выбранного изображения из images top")
    image_reconcile = image_commands.add_parser("reconcile")
    image_reconcile.add_argument("--intent-id", required=True)
    image_auto = image_commands.add_parser("auto-run", help="Один запуск автоматического сбора и публикации")
    image_auto.add_argument("--dry-run", action="store_true", help="Проверить выбор без публикации")
    schedule = image_commands.add_parser("schedule", help="Расписание самого проекта без Codex")
    schedule.add_argument("schedule_action", choices=("status", "pause", "resume", "tick"))
    caption = image_commands.add_parser("caption", help="Исправить подпись опубликованного поста")
    caption.add_argument("--intent-id", required=True)
    caption.add_argument("--revision", required=True, type=int)
    caption.add_argument("--body-file", required=True, type=Path)
    # Последовательно обрабатываем элементы: ('backup', 'restore').
    for command in ("backup", "restore"):
        sub.add_parser(command).add_argument("path", type=Path)
    # Последовательно обрабатываем элементы: image_commands.choices.values().
    for image_parser in image_commands.choices.values():
        image_parser.add_argument("--channel", default="test", help="ID профиля канала из TOPIC.md")
    portable = sub.add_parser("export-project", help="Архив кода без секретов и окружения")
    portable.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Обрабатываем ветвь при условии: args.command == 'export-project'.
        if args.command == "export-project":
            from .portability import package_project
            print(json.dumps(package_project(root, args.path), ensure_ascii=False))
            return 0
        # Обрабатываем ветвь при условии: args.command == 'channels'.
        if args.command == "channels":
            from .channel_management import configure_channel, list_channels
            from .config import parse_json

            service = build_service(root)
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                # Обрабатываем ветвь при условии: args.channel_command == 'list'.
                if args.channel_command == "list":
                    result = list_channels(service)
                # Обрабатываем ветвь при условии: args.channel_command == 'apply'.
                elif args.channel_command == "apply":
                    result = configure_channel(service, parse_json(args.file.read_text(encoding="utf-8-sig")))
                else:
                    # Альтернативная ветвь после проверки основного условия.
                    from .image_schedule import lock
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with lock(root, "run"):
                        service.connect(args.channel, args.chat_id)
                    result = {"state": "connected", "channel_id": args.channel}
                print(json.dumps(result, ensure_ascii=False))
                return 0
            finally:
                # Завершаем работу и освобождаем ресурсы независимо от результата.
                service.close()
        # Обрабатываем ветвь при условии: args.command == 'images'.
        if args.command == "images":
            from .image_commands import execute
            return execute(args, root)
        # Обрабатываем ветвь при условии: args.command == 'browser-create-channel'.
        if args.command == "browser-create-channel":
            from .browser_channels import create_channel

            load_env(root)
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                result = create_channel(Settings.load(root), args.name, args.description,
                                        bind_channel=args.bind, recover_chat_id=args.recover_chat_id)
            # Обрабатываем AgentError и выбираем допустимый исход.
            except AgentError:
                raise
            # Обрабатываем Exception и выбираем допустимый исход.
            except Exception as exc:
                raise AgentError("BROWSER_CHANNEL_CREATE_UNCONFIRMED") from exc
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["state"] == "confirmed" else 1
        # Обрабатываем ветвь при условии: args.command == 'init'.
        if args.command == "init":
            initialize(root)
            print("Локальные настройки готовы. Ключи панели находятся в .env.local.")
            return 0
        # Обрабатываем ветвь при условии: args.command == 'restore'.
        if args.command == "restore":
            from .backup import restore

            load_env(root)
            settings = Settings.load(root)
            db = Database(settings)
            db.initialize()
            service = Service(settings, db)
            key = os.getenv("MAX_AGENT_BACKUP_KEY")
            # Обрабатываем ветвь при условии: not key.
            if not key:
                raise AgentError("BACKUP_KEY_REQUIRED")
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                from .image_schedule import lock
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with lock(root, "engine"), lock(root, "run"):
                    restore(service, args.path, key.encode())
                print(json.dumps({"restored": True, "channels_paused": True}))
            finally:
                # Завершаем работу и освобождаем ресурсы независимо от результата.
                service.close()
            return 0
        service = build_service(root)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Обрабатываем ветвь при условии: args.command == 'doctor'.
            if args.command == "doctor":
                print(
                    json.dumps(
                        {
                            "status": "configured",
                            "database": service.db.engine.dialect.name,
                            "opencode": service.editor.probe(),
                            "max_mode": service.publisher.mode,
                            "enabled_channels": sum(
                                c["enabled"] for c in service.settings.topic["channels"]
                            ),
                            "production_accepted": False,
                        },
                        ensure_ascii=False,
                    )
                )
            # Обрабатываем ветвь при условии: args.command == 'tick'.
            elif args.command == "tick":
                print(json.dumps({"confirmed_publications": service.tick()}))
            # Обрабатываем ветвь при условии: args.command == 'worker'.
            elif args.command == "worker":
                import threading
                from .image_schedule import lock, run_loop

                stop = threading.Event()
                image_worker = threading.Thread(target=run_loop, args=(root, stop), daemon=True)
                image_worker.start()
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    # Продолжаем цикл, пока выполняется условие: not stop.wait(5).
                    while not stop.wait(5):
                        # Выполняем операцию с отдельной обработкой возможных ошибок.
                        try:
                            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                            with lock(root, "run"):
                                service.tick()
                        # Обрабатываем AgentError и выбираем допустимый исход.
                        except AgentError as exc:
                            # Обрабатываем ветвь при условии: exc.code != 'IMAGE_SCHEDULE_BUSY'.
                            if exc.code != "IMAGE_SCHEDULE_BUSY":
                                raise
                finally:
                    # Завершаем работу и освобождаем ресурсы независимо от результата.
                    stop.set()
                    image_worker.join(timeout=5)
            # Обрабатываем ветвь при условии: args.command == 'serve'.
            elif args.command == "serve":
                import uvicorn
                from .api import create_app

                # Обрабатываем ветвь при условии: not os.getenv(service.settings.app['api']['auth_secret_env']).
                if not os.getenv(service.settings.app["api"]["auth_secret_env"]):
                    raise AgentError("PANEL_SECRET_REQUIRED")
                uvicorn.run(
                    create_app(service, run_worker=True),
                    host=args.host or service.settings.app["api"].get("host", "127.0.0.1"),
                    port=args.port or service.settings.app["api"].get("port", 8080),
                    access_log=False,
                )
            else:
                # Альтернативная ветвь после проверки основного условия.
                from .backup import backup

                key = os.getenv("MAX_AGENT_BACKUP_KEY")
                # Обрабатываем ветвь при условии: not key.
                if not key:
                    raise AgentError("BACKUP_KEY_REQUIRED")
                # Обрабатываем ветвь при условии: args.command == 'backup'.
                if args.command == "backup":
                    from .image_schedule import lock
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with lock(root, "engine"), lock(root, "run"):
                        digest = backup(service, args.path, key.encode())
                    print(json.dumps({"sha256": digest}))
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            service.close()
        return 0
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        print(json.dumps({"code": exc.code}, ensure_ascii=False))
        return 1
    # Обрабатываем KeyboardInterrupt и выбираем допустимый исход.
    except KeyboardInterrupt:
        return 0


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    raise SystemExit(main())
