# Запуск браузерных операций через отдельный процесс.
"""Publisher adapter using a separately authenticated, project-owned browser profile."""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid

from .adapters import Delivery
from .browser_worker import PUBLISHER_INVOCATION_FIELD, _request_record, _snapshot_request, read_preclick_failure, save
from .config import AgentError, fingerprint, inside, parse_json


FINISHED_PRECLICK_CONTRACT = "browser-publisher-finished-preclick-v1"
_FINISHED_PRECLICK_INVALID = "BROWSER_FINISHED_PRECLICK_PROOF_INVALID"


def _proof_path(settings, directory, request_id):
    from .server_route_admin import plain

    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise ValueError("request ID")
    return plain(settings.root, f"data/{directory}/{request_id}.json")


def _read_proof(path, limit):
    partial = path.with_suffix(".new")
    if partial.exists() or partial.is_symlink() or partial.is_junction() or not path.is_file():
        raise ValueError("proof file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("proof size")
    value = parse_json(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("proof object")
    return value, hashlib.sha256(raw).hexdigest()


def _preparing_started_at(settings, journal, payload, publisher_invocation_id):
    started_at = journal.get("started_at")
    expected = {
        **_request_record(settings, payload),
        "contract": "browser-send-journal-v1",
        "state": "preparing",
        "send_started": False,
        "phase": "session",
        "started_at": started_at,
        PUBLISHER_INVOCATION_FIELD: publisher_invocation_id,
    }
    if (not isinstance(publisher_invocation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", publisher_invocation_id)
            or type(started_at) not in {int, float} or not math.isfinite(started_at) or started_at <= 0
            or journal.get("send_started") is not False or journal != expected):
        raise ValueError("initial journal")
    return started_at


def _ordered_stamps(invoked_at, started_at, finished_at):
    return (all(type(stamp) in {int, float} and math.isfinite(stamp) for stamp in (invoked_at, started_at, finished_at))
            and 0 < invoked_at <= started_at <= finished_at)


def _save_finished_preclick(settings, payload, publisher_invocation_id, invoked_at, finished_at, completion):
    request_id = payload["request_id"]
    try:
        journal_path = _proof_path(settings, "browser-publications", request_id)
        journal, journal_sha256 = _read_proof(journal_path, 8_388_608)
        authorized = journal["request_payload"]
        snapshot_hash = authorized.get("dispatch_snapshot_hash")
        if (not isinstance(snapshot_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
                or any(authorized.get(name) != value for name, value in payload.items())):
            return
        started_at = _preparing_started_at(settings, journal, authorized, publisher_invocation_id)
        if not _ordered_stamps(invoked_at, started_at, finished_at):
            return
        path = _proof_path(settings, "browser-publisher-receipts", request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, {
            "contract": FINISHED_PRECLICK_CONTRACT,
            "request_id": request_id,
            "config_hash": settings.hash,
            "snapshot_hash": snapshot_hash,
            PUBLISHER_INVOCATION_FIELD: publisher_invocation_id,
            "journal_sha256": journal_sha256,
            "invoked_at": invoked_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "completion": completion,
        }, exclusive=True)
    except (AgentError, OSError, ValueError, TypeError, KeyError, AttributeError):
        # Без exact proof исход остаётся unknown, как и до этой проверки.
        return


def read_finished_preclick_receipt(settings, request_id, *, snapshot, chat_id=None):
    """Локальное доказательство: завершившийся процесс не дошёл до intent отправки."""
    try:
        path = _proof_path(settings, "browser-publisher-receipts", request_id)
        receipt, sha256 = _read_proof(path, 4096)
        fields = {"contract", "request_id", "config_hash", "snapshot_hash", PUBLISHER_INVOCATION_FIELD,
                  "journal_sha256", "invoked_at", "started_at", "finished_at", "completion"}
        snapshot_hash = fingerprint(snapshot)
        if (set(receipt) != fields or receipt["contract"] != FINISHED_PRECLICK_CONTRACT
                or receipt["request_id"] != request_id or receipt["config_hash"] != settings.hash
                or receipt["snapshot_hash"] != snapshot_hash
                or receipt["completion"] not in {"exited", "timed_out_reaped"}):
            raise ValueError("publisher receipt")
        expected, presentation = _snapshot_request(settings, request_id, snapshot,
                                                   snapshot["destination"] if chat_id is None else chat_id)
        payload = {**expected, **presentation, "dispatch_snapshot_hash": snapshot_hash}
        journal_path = _proof_path(settings, "browser-publications", request_id)
        journal, journal_sha256 = _read_proof(journal_path, 8_388_608)
        started_at = _preparing_started_at(settings, journal, payload, receipt[PUBLISHER_INVOCATION_FIELD])
        if (receipt["journal_sha256"] != journal_sha256 or receipt["started_at"] != started_at
                or not _ordered_stamps(receipt["invoked_at"], receipt["started_at"], receipt["finished_at"])):
            raise ValueError("journal receipt mismatch")
        return {"receipt": receipt, "sha256": sha256, "journal_sha256": journal_sha256}
    except (AgentError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AgentError(_FINISHED_PRECLICK_INVALID) from exc


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
        request = {"action": action, "config_hash": self.settings.hash, **payload}
        invocation_id = uuid.uuid4().hex if action == "publish" else None
        worker_request = {**request, PUBLISHER_INVOCATION_FIELD: invocation_id} if invocation_id else request
        invoked_at = time.time()
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            result = subprocess.run(
                [sys.executable, "-m", "max_postiz_agent.browser_worker", "--root", str(self.settings.root)],
                input=json.dumps(worker_request, ensure_ascii=True),
                text=True, capture_output=True,
                timeout=self.settings.app["max"]["browser"].get("operation_timeout_seconds", 120) + 60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # На POSIX subprocess.run сначала завершает и дожидается процесса.
            if invocation_id is not None and os.name == "posix":
                _save_finished_preclick(self.settings, request, invocation_id, invoked_at, time.time(),
                                        "timed_out_reaped")
            raise AgentError("BROWSER_OPERATION_UNCONFIRMED") from exc
        except (OSError, ValueError) as exc:
            raise AgentError("BROWSER_OPERATION_UNCONFIRMED") from exc
        finished_at = time.time()
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            if invocation_id is not None and result.returncode != 0 and not result.stdout:
                _save_finished_preclick(self.settings, request, invocation_id, invoked_at, finished_at, "exited")
            raise AgentError("BROWSER_OPERATION_UNCONFIRMED") from exc
        if invocation_id is not None and value == {"state": "unknown"}:
            _save_finished_preclick(self.settings, request, invocation_id, invoked_at, finished_at, "exited")
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
