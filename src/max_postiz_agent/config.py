# Проверка конфигурации, идентификаторов, путей и прав.
"""Versioned configuration. External text can never change these permissions."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, FormatChecker


# Ошибка с безопасным кодом для панели и журнала.
class AgentError(Exception):
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, code: str, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)


# Формируем объект, запрещая повтор одного ключа.
def unique_keys(pairs):
    value = {}
    # Последовательно обрабатываем элементы: pairs.
    for key, item in pairs:
        # Обрабатываем ветвь при условии: key in value.
        if key in value:
            raise AgentError("DUPLICATE_JSON_KEY")
        value[key] = item
    return value


# Разбираем JSON и отклоняем неоднозначные повторяющиеся ключи.
def parse_json(text: str):
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        return json.loads(text, object_pairs_hook=unique_keys)
    # Обрабатываем (json.JSONDecodeError, ValueError) и выбираем допустимый исход.
    except (json.JSONDecodeError, ValueError) as exc:
        raise AgentError("INVALID_JSON") from exc


# Получаем стабильный хеш содержимого для контроля изменений.
def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


# Проверяем, что разрешённый путь остаётся внутри проекта.
def inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    # Обрабатываем ветвь при условии: not candidate.is_relative_to(root.resolve()).
    if not candidate.is_relative_to(root.resolve()):
        raise AgentError("PATH_OUTSIDE_PROJECT")
    return candidate


# Проверяем структуру без раскрытия значений в сообщении ошибки.
def validate_schema(root: Path, name: str, value):
    schema = parse_json((root / "schemas" / name).read_text(encoding="utf-8-sig"))
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    # Обрабатываем ветвь при условии: errors.
    if errors:
        # Validation messages may contain user-controlled secret values.
        raise AgentError("CONFIG_SCHEMA_INVALID")


# Извлекаем единственный блок настроек редакции и проверяем каналы.
def read_topic(root: Path, path="TOPIC.md"):
    text = inside(root, path).read_text(encoding="utf-8-sig")
    pattern = r"<!-- TOPIC_CONFIG_BEGIN -->\s*```json\s*(.*?)\s*```\s*<!-- TOPIC_CONFIG_END -->"
    matches = re.findall(pattern, text, re.S)
    # Обрабатываем ветвь при условии: len(matches) != 1 or text.count('<!-- TOPIC_CONFIG_BEGIN -->') != 1.
    if len(matches) != 1 or text.count("<!-- TOPIC_CONFIG_BEGIN -->") != 1:
        raise AgentError("TOPIC_BLOCK_INVALID")
    topic = parse_json(matches[0])
    validate_schema(root, "topic.schema.json", topic)
    ids = [c["id"] for c in topic["channels"]]
    # Обрабатываем ветвь при условии: len(ids) != len(set(ids)).
    if len(ids) != len(set(ids)):
        raise AgentError("DUPLICATE_CHANNEL_ID")
    # Последовательно обрабатываем элементы: topic['channels'].
    for channel in topic["channels"]:
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            ZoneInfo(channel["publication"]["timezone"])
        # Обрабатываем ZoneInfoNotFoundError и выбираем допустимый исход.
        except ZoneInfoNotFoundError as exc:
            raise AgentError("INVALID_TIMEZONE") from exc
        # Обрабатываем ветвь при условии: channel['enabled'] and 'ЗАПОЛНИТЬ' in json.dumps(channel, ensure_ascii=False).
        if channel["enabled"] and "ЗАПОЛНИТЬ" in json.dumps(channel, ensure_ascii=False):
            raise AgentError("TOPIC_NOT_FILLED")
        # Обрабатываем ветвь при условии: channel['monetization']['enabled'] and (not channel['monetization']['models']).
        if channel["monetization"]["enabled"] and not channel["monetization"]["models"]:
            raise AgentError("MONETIZATION_MODEL_REQUIRED")
    return topic


# Проверяем каталог источников и ссылки на профили каналов.
def read_sources(root, path, topic):
    data = parse_json(inside(root, path).read_text(encoding="utf-8-sig"))
    validate_schema(root, "sources.schema.json", data)
    ids = [s["id"] for s in data["sources"]]
    # Обрабатываем ветвь при условии: len(ids) != len(set(ids)).
    if len(ids) != len(set(ids)):
        raise AgentError("DUPLICATE_SOURCE_ID")
    channel_ids = {c["id"] for c in topic["channels"]}
    # Последовательно обрабатываем элементы: data['sources'].
    for source in data["sources"]:
        # Обрабатываем ветвь при условии: not set(source['channel_ids']) <= channel_ids.
        if not set(source["channel_ids"]) <= channel_ids:
            raise AgentError("UNKNOWN_CHANNEL")
        # Обрабатываем ветвь при условии: source['enabled'] and source.get('endpoint').
        if source["enabled"] and source.get("endpoint"):
            from .network import validate_url

            validate_url(source["endpoint"], resolve=False)
    return data


# Проверенная конфигурация одной установки проекта.
@dataclass
class Settings:
    root: Path
    app: dict
    topic: dict
    sources: dict

    # Читаем и проверяем конфигурацию установки.
    @classmethod
    def load(cls, root: Path, app_path="config/app.json"):
        root = root.resolve()
        app = parse_json(inside(root, app_path).read_text(encoding="utf-8-sig"))
        # Обрабатываем ветвь при условии: app.get('schema_version') != '1.0'.
        if app.get("schema_version") != "1.0":
            raise AgentError("APP_VERSION_INVALID")
        # Адрес и порт панели проверяются до следующего запуска сервера.
        api = app.get("api", {})
        if (not isinstance(api.get("host", "127.0.0.1"), str)
                or not api.get("host", "127.0.0.1")
                or isinstance(api.get("port", 8080), bool)
                or not isinstance(api.get("port", 8080), int)
                or not 1 <= api.get("port", 8080) <= 65535):
            raise AgentError("API_ADDRESS_INVALID")
        # Последовательно обрабатываем элементы: ('state_dir', 'media_dir', 'logs_dir', 'topic_path', 'sources_path').
        for key in ("state_dir", "media_dir", "logs_dir", "topic_path", "sources_path"):
            inside(root, app[key])
        # Последовательно обрабатываем элементы: ('opencode', 'max').
        for section in ("opencode", "max"):
            modes = {"fake", "live", "browser"} if section == "max" else {"fake", "live"}
            # Обрабатываем ветвь при условии: app[section]['adapter_mode'] not in modes.
            if app[section]["adapter_mode"] not in modes:
                raise AgentError("ADAPTER_MODE_INVALID")
        # Обрабатываем ветвь при условии: app['max']['adapter_mode'] == 'browser'.
        if app["max"]["adapter_mode"] == "browser":
            browser = app["max"].get("browser", {})
            profile = browser.get("profile_dir", "data/max-browser")
            # Обрабатываем ветвь при условии: not isinstance(profile, str) or not profile.startswith('data/').
            if not isinstance(profile, str) or not profile.startswith("data/"):
                raise AgentError("BROWSER_PROFILE_INVALID")
            profile_path = inside(root, profile)
            # Обрабатываем ветвь при условии: not profile_path.is_relative_to(root / 'data') or profile_path == root / 'data'.
            if not profile_path.is_relative_to(root / "data") or profile_path == root / "data":
                raise AgentError("BROWSER_PROFILE_INVALID")
            media_path = inside(root, app["media_dir"])
            # Обрабатываем ветвь при условии: profile_path.is_relative_to(media_path) or media_path.is_relative_to(profile_path).
            if profile_path.is_relative_to(media_path) or media_path.is_relative_to(profile_path):
                raise AgentError("BROWSER_PROFILE_INVALID")
            # Обрабатываем ветвь при условии: browser.get('channel', 'chrome') not in {'chrome', 'msedge', 'chromium'}.
            if browser.get("channel", "chrome") not in {"chrome", "msedge", "chromium"}:
                raise AgentError("BROWSER_CHANNEL_INVALID")
            # Обрабатываем ветвь при условии: not isinstance(browser.get('headless', True), bool).
            if not isinstance(browser.get("headless", True), bool):
                raise AgentError("BROWSER_HEADLESS_INVALID")
            timeout = browser.get("operation_timeout_seconds", 120)
            # Обрабатываем ветвь при условии: isinstance(timeout, bool) or not isinstance(timeout, int) or (not 30 <= timeout <= 300).
            if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 300:
                raise AgentError("BROWSER_TIMEOUT_INVALID")
            channels = browser.get("channels")
            # Обрабатываем ветвь при условии: not isinstance(channels, dict) or not channels or any((not re.fullmatch('-?\\d+', key) or not isinstance(na….
            if not isinstance(channels, dict) or not channels or any(
                not re.fullmatch(r"-?\d+", key) or not isinstance(name, str)
                or not name.strip() or len(name) > 300 for key, name in channels.items()
            ):
                raise AgentError("BROWSER_CHANNEL_NOT_ALLOWED")
        # Обрабатываем ветвь при условии: app['collection']['deny_private_networks'] is not True.
        if app["collection"]["deny_private_networks"] is not True:
            raise AgentError("PRIVATE_NETWORKS_MUST_BE_DENIED")
        video = app.get("video", {})
        # Обрабатываем ветвь при условии: video.get('backend', 'disabled') not in {'disabled', 'comfyui'}.
        if video.get("backend", "disabled") not in {"disabled", "comfyui"}:
            raise AgentError("VIDEO_BACKEND_INVALID")
        # Последовательно обрабатываем элементы: ('max_project_cost_rub', 'daily_generation_limit_rub', 'max_scene_cost_rub').
        for key in ("max_project_cost_rub", "daily_generation_limit_rub", "max_scene_cost_rub"):
            value = video.get(key, 0)
            # Обрабатываем ветвь при условии: isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)) or (value < 0).
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise AgentError("VIDEO_BUDGET_INVALID")
        # Обрабатываем ветвь при условии: video.get('workflow_path').
        if video.get("workflow_path"):
            inside(root, video["workflow_path"])
        # Последовательно обрабатываем элементы: (('max_duration_seconds', 60, 1, 60), ('max_bytes', 104857600, 1, 262144000), ('generation_fps', 16, 1, 60)….
        for key, default, minimum, maximum in (
            ("max_duration_seconds", 60, 1, 60),
            ("max_bytes", 104857600, 1, 262144000),
            ("generation_fps", 16, 1, 60),
            ("task_timeout_seconds", 1800, 1, 86400),
        ):
            value = video.get(key, default)
            # Обрабатываем ветвь при условии: isinstance(value, bool) or not isinstance(value, int) or (not minimum <= value <= maximum).
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise AgentError("VIDEO_LIMIT_INVALID")
        topic = read_topic(root, app["topic_path"])
        # Обрабатываем ветвь при условии: app['max']['adapter_mode'] == 'browser' and any((c['publication']['mode'] == 'autopublish' for c in topic['….
        if app["max"]["adapter_mode"] == "browser" and any(
            c["publication"]["mode"] == "autopublish" for c in topic["channels"]
        ):
            raise AgentError("BROWSER_REQUIRES_REVIEW")
        sources = read_sources(root, app["sources_path"], topic)
        return cls(root, app, topic, sources)

    # Вычисляем отпечаток настроек, от которых зависят согласования.
    @property
    def hash(self):
        value = {"topic": self.topic, "sources": self.sources}
        # Обрабатываем ветвь при условии: self.app['max']['adapter_mode'] == 'browser'.
        if self.app["max"]["adapter_mode"] == "browser":
            value["publisher"] = self.app["max"]
        return fingerprint(value)

    # Ищем профиль канала по идентификатору.
    def channel(self, channel_id):
        # Последовательно обрабатываем элементы: self.topic['channels'].
        for channel in self.topic["channels"]:
            # Обрабатываем ветвь при условии: channel['id'] == channel_id.
            if channel["id"] == channel_id:
                return channel
        raise AgentError("UNKNOWN_CHANNEL", 404)

    # Ищем источник по идентификатору.
    def source(self, source_id):
        # Последовательно обрабатываем элементы: self.sources['sources'].
        for source in self.sources["sources"]:
            # Обрабатываем ветвь при условии: source['id'] == source_id.
            if source["id"] == source_id:
                return source
        raise AgentError("UNKNOWN_SOURCE", 404)
