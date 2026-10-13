# Взаимодействие с ComfyUI, ограничение генерации и проверка результата.
"""One real generator: approved ComfyUI API workflow, async history and reconciliation."""

import copy
import os
import re
from urllib.parse import urlsplit

import httpx

from .config import AgentError, fingerprint, inside, parse_json


# Структура ComfyUI объединяет связанные данные и операции модуля.
class ComfyUI:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.options = settings.app.get("video", {})
        self.base_url = os.getenv(self.options.get("base_url_env", "COMFYUI_BASE_URL"), "").rstrip(
            "/"
        )
        self.client = httpx.Client(
            timeout=30, trust_env=False, follow_redirects=False, transport=transport
        )

    # Операция configuration: взаимодействие с comfyui, ограничение генерации и проверка результата.
    def configuration(self):
        # Обрабатываем ветвь при условии: self.options.get('backend') != 'comfyui' or not self.base_url.
        if self.options.get("backend") != "comfyui" or not self.base_url:
            raise AgentError("VIDEO_BACKEND_NOT_CONNECTED")
        parsed = urlsplit(self.base_url)
        # Обрабатываем ветвь при условии: parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or pars….
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise AgentError("GENERATOR_URL_INVALID")
        workflow_file = inside(
            self.settings.root, self.options.get("workflow_path", "config/comfy/workflow.json")
        )
        # Обрабатываем ветвь при условии: not workflow_file.is_file().
        if not workflow_file.is_file():
            raise AgentError("VIDEO_WORKFLOW_REQUIRED")
        workflow = parse_json(
            inside(
                self.settings.root, self.options.get("workflow_path", "config/comfy/workflow.json")
            ).read_text(encoding="utf-8-sig")
        )
        # Обрабатываем ветвь при условии: fingerprint(workflow) != self.options.get('workflow_sha256').
        if fingerprint(workflow) != self.options.get("workflow_sha256"):
            raise AgentError("GENERATOR_WORKFLOW_NOT_APPROVED")
        # Обрабатываем ветвь при условии: not isinstance(workflow, dict) or not workflow.
        if not isinstance(workflow, dict) or not workflow:
            raise AgentError("GENERATOR_WORKFLOW_INVALID")
        allowed = self.options.get("allowed_node_classes", [])
        # Последовательно обрабатываем элементы: workflow.values().
        for node in workflow.values():
            # Обрабатываем ветвь при условии: not isinstance(node, dict) or not isinstance(node.get('inputs'), dict) or node.get('class_type') not in all….
            if (
                not isinstance(node, dict)
                or not isinstance(node.get("inputs"), dict)
                or node.get("class_type") not in allowed
            ):
                raise AgentError("GENERATOR_NODE_NOT_APPROVED")
        return workflow

    # Фиксируем настройки, для которых подтверждено подключение.
    @property
    def binding_hash(self):
        return fingerprint({"base_url": self.base_url, "options": self.options})

    # Возвращаем состояние без выполнения публикации.
    def status(self):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            self.configuration()
        # Обрабатываем (AgentError, OSError) и выбираем допустимый исход.
        except (AgentError, OSError):
            return {"state": "needs_configuration", "backend": "comfyui", "live_verified": False}
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            r = self.client.get(self.base_url + "/system_stats")
            return {
                "state": "available" if r.status_code == 200 else "unavailable",
                "backend": "comfyui",
                "live_verified": False,
            }
        # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
        except httpx.HTTPError:
            return {"state": "unavailable", "backend": "comfyui", "live_verified": False}

    # Подготавливаем проверенный материал к следующему этапу.
    def prepare(self, scene, seed):
        workflow = copy.deepcopy(self.configuration())
        values = {
            "positive": scene["prompt"],
            "negative": scene.get("negative_prompt", ""),
            "seed": seed,
            "frames": max(
                1, round(scene["duration_seconds"] * self.options.get("generation_fps", 16))
            ),
        }
        # Последовательно обрабатываем элементы: self.options.get('bindings', []).
        for binding in self.options.get("bindings", []):
            node, field, value = binding["node"], binding["input"], binding["value"]
            # Обрабатываем ветвь при условии: node not in workflow or field not in workflow[node]['inputs'] or value not in values.
            if node not in workflow or field not in workflow[node]["inputs"] or value not in values:
                raise AgentError("WORKFLOW_BINDING_INVALID")
            workflow[node]["inputs"][field] = values[value]
        # Обрабатываем ветвь при условии: not any((b.get('value') == 'positive' for b in self.options.get('bindings', []))).
        if not any(b.get("value") == "positive" for b in self.options.get("bindings", [])):
            raise AgentError("WORKFLOW_PROMPT_BINDING_REQUIRED")
        return workflow

    # Отправляем задачу генерации после проверки лимитов и согласования.
    def submit(self, scene, request_id, seed):
        workflow = self.prepare(scene, seed)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            r = self.client.post(
                self.base_url + "/prompt",
                json={
                    "prompt": workflow,
                    "client_id": request_id,
                    "extra_data": {"max_request_id": request_id},
                },
            )
        # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
        except httpx.HTTPError as exc:
            raise AgentError("VIDEO_SUBMIT_UNKNOWN") from exc
        # Обрабатываем ветвь при условии: r.status_code == 400.
        if r.status_code == 400:
            raise AgentError("VIDEO_WORKFLOW_REJECTED")
        # Обрабатываем ветвь при условии: r.status_code != 200.
        if r.status_code != 200:
            raise AgentError("VIDEO_SUBMIT_UNKNOWN")
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            remote_id = r.json()["prompt_id"]
            # Обрабатываем ветвь при условии: not isinstance(remote_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', remote_id).
            if not isinstance(remote_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", remote_id
            ):
                raise ValueError
            return remote_id
        # Обрабатываем (ValueError, KeyError, TypeError) и выбираем допустимый исход.
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError("VIDEO_SUBMIT_UNKNOWN") from exc

    # Запрашиваем результат уже отправленной задачи.
    def poll(self, remote_id):
        # Обрабатываем ветвь при условии: not isinstance(remote_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', remote_id).
        if not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", remote_id):
            raise AgentError("GENERATOR_JOB_ID_INVALID")
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            r = self.client.get(self.base_url + "/history/" + remote_id)
            r.raise_for_status()
            history = r.json().get(remote_id)
        # Обрабатываем (httpx.HTTPError, ValueError, AttributeError) и выбираем допустимый исход.
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise AgentError("VIDEO_HISTORY_UNAVAILABLE") from exc
        # Обрабатываем ветвь при условии: not history.
        if not history:
            return {"state": "running"}
        # Обрабатываем ветвь при условии: history.get('status', {}).get('status_str') == 'error'.
        if history.get("status", {}).get("status_str") == "error":
            return {"state": "failed"}
        outputs = history.get("outputs", {})
        # Последовательно обрабатываем элементы: outputs.values().
        for output in outputs.values():
            # Последовательно обрабатываем элементы: ('videos', 'gifs', 'images').
            for field in ("videos", "gifs", "images"):
                # Последовательно обрабатываем элементы: output.get(field, []).
                for asset in output.get(field, []):
                    # Обрабатываем ветвь при условии: asset.get('filename', '').lower().endswith('.mp4').
                    if asset.get("filename", "").lower().endswith(".mp4"):
                        return {"state": "completed", "asset": asset}
        # Обрабатываем ветвь при условии: history.get('status', {}).get('completed').
        if history.get("status", {}).get("completed"):
            return {"state": "failed"}
        return {"state": "running"}

    # Сверяем сохранённую попытку без повторного нажатия отправки.
    def reconcile(self, request_id):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            history = self.client.get(self.base_url + "/history")
            queue = self.client.get(self.base_url + "/queue")
            history.raise_for_status()
            queue.raise_for_status()
            prompts = [v.get("prompt", []) for v in history.json().values()]
            q = queue.json()
            prompts.extend(q.get("queue_running", []) + q.get("queue_pending", []))
            ids = {
                p[1]
                for p in prompts
                if isinstance(p, list)
                and len(p) > 3
                and isinstance(p[3], dict)
                and p[3].get("max_request_id") == request_id
            }
            return next(iter(ids)) if len(ids) == 1 else None
        # Обрабатываем (httpx.HTTPError, ValueError, AttributeError, TypeError) и выбираем допустимый исход.
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            raise AgentError("VIDEO_RECONCILE_UNAVAILABLE") from exc

    # Операция download: взаимодействие с comfyui, ограничение генерации и проверка результата.
    def download(self, asset, relative):
        name, subfolder = asset.get("filename", ""), asset.get("subfolder", "")
        # Обрабатываем ветвь при условии: not re.fullmatch('[\\w .-]+\\.mp4', name, re.I) or any((x in subfolder for x in ('..', '\\', ':'))) or subf….
        if (
            not re.fullmatch(r"[\w .-]+\.mp4", name, re.I)
            or any(x in subfolder for x in ("..", "\\", ":"))
            or subfolder.startswith("/")
        ):
            raise AgentError("GENERATOR_OUTPUT_PATH_INVALID")
        path = inside(self.settings.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        size = 0
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.client.stream(
                "GET",
                self.base_url + "/view",
                params={"filename": name, "subfolder": subfolder, "type": "output"},
            ) as r:
                r.raise_for_status()
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with temporary.open("wb") as stream:
                    # Последовательно обрабатываем элементы: r.iter_bytes().
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        # Обрабатываем ветвь при условии: size > self.options.get('max_bytes', 104857600).
                        if size > self.options.get("max_bytes", 104857600):
                            raise AgentError("VIDEO_TOO_LARGE")
                        stream.write(chunk)
            temporary.replace(path)
        # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
        except httpx.HTTPError as exc:
            raise AgentError("VIDEO_DOWNLOAD_FAILED") from exc
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            temporary.unlink(missing_ok=True)
        return relative

    # Освобождаем соединения и принадлежащие объекту ресурсы.
    def close(self):
        self.client.close()
