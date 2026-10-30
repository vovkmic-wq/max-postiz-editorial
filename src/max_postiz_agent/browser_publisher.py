# Запуск браузерных операций через отдельный процесс.
"""Publisher adapter using a separately authenticated, project-owned browser profile."""

import hashlib
import json
import os
import subprocess
import sys

from .adapters import Delivery
from .browser_worker import read_preclick_failure
from .config import AgentError, fingerprint, inside


# Адаптер публикации через собственный браузерный профиль проекта.
class BrowserMax:
    mode = "browser"
    token = ""

    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, settings):
        # Обрабатываем ветвь при условии: settings.app['max']['adapter_mode'] != 'browser'.
        if settings.app["max"]["adapter_mode"] != "browser":
            raise AgentError("BROWSER_MODE_NOT_ENABLED")
        self.settings = settings

    # Выполняем ограниченный запрос и проверяем формат ответа.
    def call(self, action, **payload):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            result = subprocess.run(
                [sys.executable, "-m", "max_postiz_agent.browser_worker", "--root", str(self.settings.root)],
                input=json.dumps({"action": action, "config_hash": self.settings.hash, **payload}, ensure_ascii=True),
                text=True, capture_output=True,
                timeout=self.settings.app["max"]["browser"].get("operation_timeout_seconds", 120) + 60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
            value = json.loads(result.stdout)
        # Обрабатываем (OSError, subprocess.TimeoutExpired, ValueError) и выбираем допустимый исход.
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise AgentError("BROWSER_OPERATION_UNCONFIRMED") from exc
        # Обрабатываем ветвь при условии: not isinstance(value, dict).
        if not isinstance(value, dict):
            raise AgentError("BROWSER_OPERATION_UNCONFIRMED")
        # Обрабатываем ветвь при условии: 'code' in value.
        if "code" in value:
            raise AgentError(value["code"])
        return value

    # Проверяем доступность ресурса и его разрешённые свойства.
    def inspect(self, chat_id):
        return {**self.call("inspect", chat_id=chat_id),
                "binding_hash": fingerprint(self.settings.app["max"])}

    # Открываем отдельный профиль для ручного входа владельца.
    def login(self, chat_id):
        from .browser_runtime import channel_name

        channel_name(self.settings, chat_id)
        log_dir = inside(self.settings.root, "data")
        log_dir.mkdir(parents=True, exist_ok=True)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with (log_dir / "browser-login.stdout.log").open("w") as out, (log_dir / "browser-login.stderr.log").open("w") as err:
            process = subprocess.Popen(
                [sys.executable, "-m", "max_postiz_agent.browser_login", "--root",
                 str(self.settings.root), "--chat-id=" + chat_id], stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return {"state": "login_window_requested", "pid": process.pid}

    # Выполняем отправку и возвращаем подтверждённый либо неопределённый результат.
    def publish(self, chat_id, text, attachment=None, *, request_id=None):
        # Обрабатываем ветвь при условии: not request_id.
        if not request_id:
            raise AgentError("BROWSER_REQUEST_ID_REQUIRED")
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            value = self.call("publish", chat_id=chat_id, text=text,
                              attachment=attachment, request_id=request_id)
        except AgentError:
            # Сам по себе код ошибки не доказывает, было ли внешнее нажатие отправки.
            return Delivery("unknown")
        if value.get("state") == "preclick_failed":
            try:
                proof = read_preclick_failure(self.settings, request_id, chat_id=chat_id,
                    text=text, attachment=attachment)
                if value.get("proof_sha256") != proof["sha256"]:
                    return Delivery("unknown")
            except AgentError:
                return Delivery("unknown")
            # Hub сохраняет завершённый отказ; это не поручение повторить отправку.
            raise AgentError("BROWSER_PRECLICK_FAILED")
        return Delivery(value["state"], value.get("external_id"), value.get("permalink"))

    # Сверяем сохранённую попытку без повторного нажатия отправки.
    def reconcile(self, chat_id, request_id):
        value = self.call("reconcile", chat_id=chat_id, request_id=request_id)
        return Delivery(value["state"], value.get("external_id"), value.get("permalink"))

    # Подготавливаем проверенный ролик для выбранного адаптера.
    def upload_video(self, path):
        path = inside(self.settings.root, str(path))
        # Обрабатываем ветвь при условии: path.suffix.lower() != '.mp4'.
        if path.suffix.lower() != ".mp4":
            raise AgentError("VIDEO_MP4_REQUIRED")
        return {"type": "browser-video", "payload": {
            "path": path.relative_to(self.settings.root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }}

    # Подготавливаем проверенное изображение для выбранного адаптера.
    def upload_image(self, path):
        from .media import inspect_image

        path = inside(self.settings.root, str(path))
        info = inspect_image(self.settings.root, path.relative_to(self.settings.root).as_posix())
        return {"type": "browser-image", "payload": {
            "path": path.relative_to(self.settings.root).as_posix(),
            "sha256": info["sha256"],
        }}

    # Операция messages: запуск браузерных операций через отдельный процесс.
    def messages(self, chat_id, **params):
        raise AgentError("BROWSER_SOURCE_COLLECTION_NOT_SUPPORTED")

    # Операция correct: запуск браузерных операций через отдельный процесс.
    def correct(self, message_id, text=None, remove=False):
        raise AgentError("BROWSER_CORRECTION_NOT_SUPPORTED")

    # Освобождаем соединения и принадлежащие объекту ресурсы.
    def close(self):
        pass
