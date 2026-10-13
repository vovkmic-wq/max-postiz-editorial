"""Команды управления изображениями: общий вход для CLI и необязательного Codex."""

import json
import os

from .config import AgentError, Settings
from .db import Database


# Выполняем выбранную команду изображений и возвращаем код завершения.
def execute(args, root):
    from .cli import build_service, load_env

    from .image_pipeline import auto_run, latest, publish_rank, scan, source_config
    from .image_schedule import lock

    load_env(root)
    # Обрабатываем ветвь при условии: args.image_command == 'schedule'.
    if args.image_command == "schedule":
        from .image_schedule import configure, status, tick

        # Обрабатываем ветвь при условии: args.schedule_action == 'status'.
        if args.schedule_action == "status":
            result = status(root, channel_id=args.channel)
        # Обрабатываем ветвь при условии: args.schedule_action == 'tick'.
        elif args.schedule_action == "tick":
            result = tick(root, channel_id=args.channel)
        else:
            # Альтернативная ветвь после проверки основного условия.
            result = configure(root, args.schedule_action == "resume", channel_id=args.channel)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("schedule_state") == "requires_attention" or result["state"] in {
            "requires_attention", "error"
        } else 0
    # Обрабатываем ветвь при условии: args.image_command == 'scan'.
    if args.image_command == "scan":
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with lock(root, "run"):
            result = scan(root, all_sources=args.all_sources, exclude_used=args.unused, channel_id=args.channel)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] == "ready" else 1
    # Обрабатываем ветвь при условии: args.image_command == 'top'.
    if args.image_command == "top":
        report = latest(root, args.channel)
        print(json.dumps({"state": report["state"], "top": report["top"]}, ensure_ascii=False))
        return 0 if report["state"] == "ready" else 1
    # Обрабатываем ветвь при условии: args.image_command == 'status'.
    if args.image_command == "status":
        from sqlalchemy import select
        from .db import Draft, Intent

        config = source_config(root, args.channel)
        report = latest(root, args.channel)
        settings = Settings.load(root)
        db = Database(settings)
        db.initialize()
        publications = []
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with db.session() as session:
                # Последовательно обрабатываем элементы: session.scalars(select(Draft).where(Draft.channel_id == args.channel)).
                for draft in session.scalars(select(Draft).where(Draft.channel_id == args.channel)):
                    # Обрабатываем ветвь при условии: draft.current.get('image').
                    if draft.current.get("image"):
                        intent = session.scalar(select(Intent).where(Intent.draft_id == draft.id))
                        publications.append({"draft_id": draft.id, "state": draft.state,
                                             "intent_id": intent.id if intent else None,
                                             "delivery_state": intent.state if intent else None,
                                             "sha256": draft.current["image"]["sha256"]})
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            db.engine.dispose()
        result = {"state": report["state"], "checked_at": report.get("checked_at"),
                  "top_count": len(report["top"]), "rejected": report.get("rejected", {}),
                  "publications": publications,
                      "sources": [{"id": source["id"], "enabled": source["enabled"],
                                  "key_available": (bool(os.getenv(source["api_key_env"]))
                                                    if source["type"] == "pixabay_api" else True)}
                              for source in config["sources"]]}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    service = build_service(root)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Обрабатываем ветвь при условии: args.image_command == 'caption'.
        if args.image_command == "caption":
            from .browser_corrections import correct_caption

            result = correct_caption(service, args.intent_id, args.revision,
                                     args.body_file.read_text(encoding="utf-8-sig"))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["state"] in {"confirmed", "already_corrected"} else 1
        # Обрабатываем ветвь при условии: args.image_command == 'auto-run'.
        if args.image_command == "auto-run":
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with lock(root, "run"):
                result = auto_run(service, dry_run=args.dry_run, channel_id=args.channel)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["state"] in {"confirmed", "dry_run_ready", "no_new_image",
                                            "already_attempted", "already_published_in_slot"} else 1
        # Обрабатываем ветвь при условии: args.image_command == 'reconcile'.
        if args.image_command == "reconcile":
            from sqlalchemy import select
            from .db import Draft, Intent

            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with service.db.session() as session:
                intent = session.get(Intent, args.intent_id)
                draft = session.get(Draft, intent.draft_id) if intent else None
                # Обрабатываем ветвь при условии: not draft or draft.channel_id != args.channel or (not draft.current.get('image')).
                if not draft or draft.channel_id != args.channel or not draft.current.get("image"):
                    raise AgentError("IMAGE_INTENT_NOT_FOUND")
            result = {"intent_id": args.intent_id, "state": service.reconcile(args.intent_id)}
        else:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with lock(root, "run"):
                result = publish_rank(service, args.rank, args.sha256, channel_id=args.channel)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] == "confirmed" else 1
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        service.close()
