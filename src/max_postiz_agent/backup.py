# Зашифрованное резервирование и проверка состояния перед восстановлением.
"""Encrypted, integrity-checked portable state; restore only into an empty installation."""

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile

from cryptography.fernet import Fernet, InvalidToken

from .config import AgentError, inside
from .db import Channel, Intent, Job


# Выбираем историю и оригиналы, исключая cookies и временные данные.
def runtime_files(root):
    # Переносим журналы отправки и оригиналы, но исключаем cookies, ключи и API-кэш.
    for directory in ("data/image-ranking", "data/image-schedule",
                      "data/browser-publications", "data/browser-corrections"):
        # Последовательно обрабатываем элементы: inside(root, directory).rglob('*').
        for path in inside(root, directory).rglob("*"):
            relative = path.relative_to(root).as_posix()
            # Обрабатываем ветвь при условии: path.is_file() and (not path.is_symlink()) and ('/api-cache/' not in relative) and ('/tests/' not in relati….
            if (path.is_file() and not path.is_symlink()
                    and "/api-cache/" not in relative and "/tests/" not in relative
                    and path.name != "engine.json" and path.suffix in {".json", ".png", ".jpg", ".jpeg", ".mp4"}):
                yield relative, path.read_bytes()


# Ограничиваем допустимые назначения файлов восстановленного архива.
def portable_name(name):
    # Восстановление имеет ограниченный набор назначений даже при корректном шифровании.
    return name in {"database.json", "TOPIC.md", "config/sources.json", "config/app.json",
                    "config/image_sources.json", "config/image_channels.json", "config/holidays.json", "config/comfy/workflow.json", "config/publishing.json"} or name.startswith((
                        "media/", "data/image-ranking/", "data/image-schedule/",
                        "data/browser-publications/", "data/browser-corrections/", "data/archive/", "data/exports/"))


# Запрещаем включение значений ключей в переносимые настройки.
def check_config_secrets(value):
    # В переносимых настройках допустимы имена переменных среды, но не их значения.
    if isinstance(value, dict):
        # Последовательно обрабатываем элементы: value.items().
        for key, child in value.items():
            # Обрабатываем ветвь при условии: re.search('token|password|secret|api_key|cookie', key, re.I) and (not key.endswith('_env')) and isinstance(….
            if (re.search(r"token|password|secret|api_key|cookie", key, re.I)
                    and not key.endswith("_env") and isinstance(child, str) and child):
                raise AgentError("BACKUP_CONFIG_CONTAINS_SECRET")
            check_config_secrets(child)
    # Обрабатываем ветвь при условии: isinstance(value, list).
    elif isinstance(value, list):
        # Последовательно обрабатываем элементы: value.
        for child in value:
            check_config_secrets(child)


# Сохраняем зашифрованный снимок остановленной установки.
def backup(service, destination, key):
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        # Обрабатываем ветвь при условии: any((not c.paused for c in session.query(Channel).all())).
        if any(not c.paused for c in session.query(Channel).all()):
            raise AgentError("BACKUP_REQUIRES_PAUSE")
        # Обрабатываем ветвь при условии: session.query(Job).filter(Job.state == 'leased').first().
        if session.query(Job).filter(Job.state == "leased").first():
            raise AgentError("BACKUP_REQUIRES_STOPPED_WORKER")
        from .hub_models import BridgePost, Dispatch, RecurringTask, Target
        if session.query(Target).filter(Target.paused.is_(False)).first():
            raise AgentError("BACKUP_REQUIRES_PAUSE")
        if session.query(Dispatch).filter(Dispatch.state == "sending").first() or session.query(RecurringTask).filter(RecurringTask.owner.is_not(None)).first():
            raise AgentError("BACKUP_REQUIRES_STOPPED_WORKER")
        if session.query(BridgePost).filter(BridgePost.mutation_owner.is_not(None)).first():
            raise AgentError("BACKUP_REQUIRES_STOPPED_WORKER")
    buffer = io.BytesIO()
    files = {
        "database.json": json.dumps(service.db.export(), ensure_ascii=False).encode(),
        "TOPIC.md": (service.settings.root / service.settings.app["topic_path"]).read_bytes(),
        "config/sources.json": (
            service.settings.root / service.settings.app["sources_path"]
        ).read_bytes(),
    }
    # Последовательно обрабатываем элементы: ('config/app.json', 'config/image_sources.json', 'config/image_channels.json', 'config/comfy/workflow.json').
    for name in ("config/app.json", "config/image_sources.json", "config/image_channels.json", "config/holidays.json",
                 "config/comfy/workflow.json", "config/publishing.json"):
        path = inside(service.settings.root, name)
        # Обрабатываем ветвь при условии: path.exists().
        if path.exists():
            value = path.read_bytes()
            check_config_secrets(json.loads(value))
            files[name] = value
    files.update(runtime_files(service.settings.root))
    for directory in ("data/archive", "data/exports"):
        folder = inside(service.settings.root, directory)
        if folder.exists():
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink() and path.suffix in {".bin", ".json", ".txt", ".html", ".png", ".jpg", ".mp4"}:
                    files[path.relative_to(service.settings.root).as_posix()] = path.read_bytes()
    media_dir = service.settings.root / service.settings.app["media_dir"]
    # Обрабатываем ветвь при условии: media_dir.exists().
    if media_dir.exists():
        # Последовательно обрабатываем элементы: media_dir.rglob('*').
        for path in media_dir.rglob("*"):
            # Обрабатываем ветвь при условии: path.is_file() and (not path.is_symlink()).
            if path.is_file() and not path.is_symlink():
                files["media/" + path.relative_to(media_dir).as_posix()] = path.read_bytes()
    manifest = {
        "schema_version": "1.0",
        "hashes": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
    }
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Последовательно обрабатываем элементы: files.items().
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr("manifest.json", json.dumps(manifest))
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        encrypted = Fernet(key).encrypt(buffer.getvalue())
    # Обрабатываем (ValueError, TypeError) и выбираем допустимый исход.
    except (ValueError, TypeError) as exc:
        raise AgentError("BACKUP_KEY_INVALID") from exc
    destination.write_bytes(encrypted)
    # Обрабатываем ветвь при условии: os.name != 'nt'.
    if os.name != "nt":
        destination.chmod(0o600)
    return hashlib.sha256(encrypted).hexdigest()


# Проверяем архив и восстанавливаем его только в пустую базу.
def restore(service, archive_path, key):
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        decrypted = Fernet(key).decrypt(archive_path.read_bytes())
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with zipfile.ZipFile(io.BytesIO(decrypted)) as archive:
            names = archive.namelist()
            # Обрабатываем ветвь при условии: len(names) != len(set(names)).
            if len(names) != len(set(names)):
                raise AgentError("BACKUP_INVALID")
            manifest = json.loads(archive.read("manifest.json"))
            # Обрабатываем ветвь при условии: manifest.get('schema_version') != '1.0'.
            if manifest.get("schema_version") != "1.0":
                raise AgentError("BACKUP_SCHEMA_MISMATCH")
            # Обрабатываем ветвь при условии: set(names) != set(manifest['hashes']) | {'manifest.json'}.
            if set(names) != set(manifest["hashes"]) | {"manifest.json"}:
                raise AgentError("BACKUP_INVALID")
            content = {}
            # Последовательно обрабатываем элементы: manifest['hashes'].items().
            for name, expected in manifest["hashes"].items():
                inside(service.settings.root, name)
                # Обрабатываем ветвь при условии: not portable_name(name).
                if not portable_name(name):
                    raise AgentError("BACKUP_INVALID")
                value = archive.read(name)
                # Обрабатываем ветвь при условии: hashlib.sha256(value).hexdigest() != expected.
                if hashlib.sha256(value).hexdigest() != expected:
                    raise AgentError("BACKUP_HASH_MISMATCH")
                content[name] = value
            database = json.loads(content["database.json"])
    # Обрабатываем (InvalidToken, ValueError, zipfile.BadZipFile, KeyError) и выбираем допустимый исход.
    except (InvalidToken, ValueError, zipfile.BadZipFile, KeyError) as exc:
        raise AgentError("BACKUP_INVALID") from exc
    # Restored settings are validated before any database or configuration mutation.
    from .config import parse_json, validate_schema

    topic_text = content["TOPIC.md"].decode("utf-8-sig")
    import re

    match = re.search(r"```json\s*(.*?)\s*```", topic_text, re.S)
    # Обрабатываем ветвь при условии: not match.
    if not match:
        raise AgentError("BACKUP_TOPIC_INVALID")
    validate_schema(service.settings.root, "topic.schema.json", parse_json(match[1]))
    validate_schema(
        service.settings.root,
        "sources.schema.json",
        parse_json(content["config/sources.json"].decode("utf-8-sig")),
    )
    if "config/publishing.json" in content:
        validate_schema(service.settings.root, "publishing.schema.json", parse_json(content["config/publishing.json"].decode("utf-8-sig")))
    # Обрабатываем ветвь при условии: 'config/image_channels.json' in content.
    if "config/image_channels.json" in content:
        validate_schema(service.settings.root, "image_channels.schema.json",
                        parse_json(content["config/image_channels.json"].decode("utf-8-sig")))
    # Пути новой установки сохраняем: база уже открыта в её собственном каталоге состояния.
    if "config/app.json" in content:
        restored_app = parse_json(content["config/app.json"].decode("utf-8-sig"))
        # Последовательно обрабатываем элементы: ('state_dir', 'media_dir', 'logs_dir', 'topic_path', 'sources_path').
        for field in ("state_dir", "media_dir", "logs_dir", "topic_path", "sources_path"):
            restored_app[field] = service.settings.app[field]
        content["config/app.json"] = json.dumps(restored_app, ensure_ascii=False).encode()
    # Проверяем связанные файлы в изолированном каталоге до первого изменения базы.
    from .config import Settings
    with tempfile.TemporaryDirectory(prefix="max-restore-") as directory:
        from pathlib import Path
        validation_root = Path(directory)
        shutil.copytree(service.settings.root / "schemas", validation_root / "schemas")
        app = json.loads(content.get("config/app.json", json.dumps(service.settings.app).encode()))
        validation_files = {"config/app.json": json.dumps(app).encode(),
                            app["topic_path"]: content["TOPIC.md"],
                            app["sources_path"]: content["config/sources.json"]}
        validation_files.update({name: value for name, value in content.items()
                                 if name in {"config/image_sources.json", "config/image_channels.json", "config/holidays.json"}})
        # Последовательно обрабатываем элементы: validation_files.items().
        for name, value in validation_files.items():
            path = inside(validation_root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        candidate = Settings.load(validation_root)
        if "config/holidays.json" in content:
            from .holidays import catalog
            catalog(validation_root)
        # Обрабатываем ветвь при условии: 'config/image_sources.json' in content.
        if "config/image_sources.json" in content:
            from .image_pipeline import source_config
            from .channel_profiles import profiles
            source_config(validation_root)
            # Последовательно обрабатываем элементы: profiles(validation_root)['channels'].
            for channel_id in profiles(validation_root)["channels"]:
                source_config(validation_root, channel_id)
        del candidate
    service.db.restore(database)
    (service.settings.root / service.settings.app["topic_path"]).write_bytes(content["TOPIC.md"])
    (service.settings.root / service.settings.app["sources_path"]).write_bytes(
        content["config/sources.json"]
    )
    # Последовательно обрабатываем элементы: content.items().
    for name, value in content.items():
        # Обрабатываем ветвь при условии: name.startswith('media/').
        if name.startswith("media/"):
            path = inside(service.settings.root, service.settings.app["media_dir"] + "/" + name[6:])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        # Обрабатываем ветвь при условии: name.startswith(('config/', 'data/')) and name != 'config/sources.json'.
        elif name.startswith(("config/", "data/")) and name != "config/sources.json":
            # После переноса отправка отключена до проверки нового браузера и модели.
            if name.startswith("data/image-schedule/") and name.endswith("/control.json"):
                control = json.loads(value)
                control.update(enabled=False, reason="restored_on_new_installation")
                value = json.dumps(control, ensure_ascii=False).encode()
            path = inside(service.settings.root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as session:
        from .hub_models import Dispatch, Target
        for target in session.query(Target).all():
            target.paused = True
        for dispatch in session.query(Dispatch).filter(Dispatch.state == "sending"):
            dispatch.state, dispatch.owner, dispatch.lease_until = "unknown", None, 0
        # Последовательно обрабатываем элементы: session.query(Channel).all().
        for channel in session.query(Channel).all():
            channel.paused = True
        # Последовательно обрабатываем элементы: session.query(Intent).filter(Intent.state == 'sending').
        for intent in session.query(Intent).filter(Intent.state == "sending"):
            intent.state = "unknown"
        # Последовательно обрабатываем элементы: session.query(Job).filter(Job.state == 'leased').
        for job in session.query(Job).filter(Job.state == "leased"):
            job.state, job.lease_until, job.owner = "queued", 0, None
    return manifest
