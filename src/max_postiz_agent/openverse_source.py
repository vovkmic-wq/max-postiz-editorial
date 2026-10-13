# Сбор разрешённых изображений Openverse и проверка лицензий.
"""Bounded access to openly licensed images from distinct Openverse source sites."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from .config import AgentError, inside, parse_json
from .network import validate_url


API = "https://api.openverse.org/v1/images/"
LICENSES = {"cc0", "pdm", "by"}
ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CACHE = "data/image-ranking/openverse-cache"


# Загружаем данные с фиксированного разрешённого адреса.
def _fetch(url, *, image=False):
    """Only fixed Openverse API URLs are fetched; media stays behind its proxy."""
    # Обрабатываем ветвь при условии: not url.startswith(API) or urlsplit(url).hostname != 'api.openverse.org'.
    if not url.startswith(API) or urlsplit(url).hostname != "api.openverse.org":
        raise AgentError("OPENVERSE_URL_INVALID")
    limit = 10_485_760 if image else 1_048_576
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=True) as client:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with client.stream("GET", url, headers={"User-Agent": "MAXImageAgent/0.1"}) as response:
                # Обрабатываем ветвь при условии: response.status_code in {403, 429}.
                if response.status_code in {403, 429}:
                    raise AgentError(f"SOURCE_HTTP_{response.status_code}")
                # Обрабатываем ветвь при условии: response.status_code != 200.
                if response.status_code != 200:
                    raise AgentError("OPENVERSE_RESPONSE_INVALID")
                mime = response.headers.get("content-type", "").split(";")[0].lower()
                # Обрабатываем ветвь при условии: mime not in ({'image/jpeg', 'image/png'} if image else {'application/json'}).
                if mime not in ({"image/jpeg", "image/png"} if image else {"application/json"}):
                    raise AgentError("OPENVERSE_FORMAT_INVALID")
                chunks, size = [], 0
                # Последовательно обрабатываем элементы: response.iter_bytes().
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    # Обрабатываем ветвь при условии: size > limit.
                    if size > limit:
                        raise AgentError("SOURCE_TOO_LARGE")
                    chunks.append(chunk)
                return b"".join(chunks)
    # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
    except httpx.HTTPError as exc:
        raise AgentError("OPENVERSE_TRANSPORT_ERROR") from exc


# Возвращаем проверенный кэш либо сохраняем свежий ответ.
def cached_json(root, path, params=None):
    url = API + path + ("?" + urlencode(params) if params else "")
    key = hashlib.sha256(url.encode()).hexdigest()
    cache = inside(root, f"{CACHE}/{key}.json")
    # Обрабатываем ветвь при условии: cache.exists().
    if cache.exists():
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            saved = parse_json(cache.read_text(encoding="utf-8"))
            # Обрабатываем ветвь при условии: datetime.now(timezone.utc) - datetime.fromisoformat(saved['checked_at']) < timedelta(hours=24).
            if datetime.now(timezone.utc) - datetime.fromisoformat(saved["checked_at"]) < timedelta(hours=24):
                return saved["result"]
        # Обрабатываем (ValueError, TypeError, KeyError) и выбираем допустимый исход.
        except (ValueError, TypeError, KeyError):
            pass
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        result = json.loads(_fetch(url))
    # Обрабатываем ValueError и выбираем допустимый исход.
    except ValueError as exc:
        raise AgentError("OPENVERSE_RESPONSE_INVALID") from exc
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                                 "result": result}, ensure_ascii=False), encoding="utf-8")
    return result


# Получаем поисковые результаты и исключаем повтор идентификаторов.
def candidates(root, source):
    result = cached_json(root, "", {"q": source["query"], "source": source["source_name"],
                                    "license": "cc0,pdm,by", "page_size": source["per_query"],
                                    "mature": "false"})
    # Обрабатываем ветвь при условии: not isinstance(result, dict) or not isinstance(result.get('results'), list).
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise AgentError("OPENVERSE_RESPONSE_INVALID")
    return result["results"]


# Проверяем актуальные данные источника перед использованием.
def verify(root, source, item):
    identifier = str(item.get("id", ""))
    # Обрабатываем ветвь при условии: not ID_PATTERN.fullmatch(identifier).
    if not ID_PATTERN.fullmatch(identifier):
        return None, "openverse_id_invalid"
    detail = cached_json(root, identifier + "/")
    # Обрабатываем ветвь при условии: not isinstance(detail, dict) or detail.get('source') != source['source_name'].
    if not isinstance(detail, dict) or detail.get("source") != source["source_name"]:
        return None, "openverse_source_changed"
    # Обрабатываем ветвь при условии: detail.get('license') not in LICENSES.
    if detail.get("license") not in LICENSES:
        return None, "openverse_license_invalid"
    license_url = detail.get("license_url", "")
    license_host = urlsplit(license_url).hostname
    expected = {"by": "/licenses/by/", "cc0": "/publicdomain/zero/",
                "pdm": "/publicdomain/mark/"}[detail["license"]]
    # Обрабатываем ветвь при условии: license_host != 'creativecommons.org' or not urlsplit(license_url).path.startswith(expected).
    if license_host != "creativecommons.org" or not urlsplit(license_url).path.startswith(expected):
        return None, "openverse_license_invalid"
    # Обрабатываем ветвь при условии: detail['license'] == 'by' and (not detail.get('creator')).
    if detail["license"] == "by" and not detail.get("creator"):
        return None, "openverse_creator_missing"
    page_url = detail.get("foreign_landing_url", "")
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        validate_url(page_url, resolve=False)
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError:
        return None, "openverse_page_invalid"
    # Обрабатываем ветвь при условии: urlsplit(page_url).scheme != 'https'.
    if urlsplit(page_url).scheme != "https":
        return None, "openverse_page_invalid"
    page_host = urlsplit(page_url).hostname or ""
    site_host = (urlsplit(source["site_url"]).hostname or "").removeprefix("www.")
    # Обрабатываем ветвь при условии: page_host != site_host and (not page_host.endswith('.' + site_host)).
    if page_host != site_host and not page_host.endswith("." + site_host):
        return None, "openverse_page_source_mismatch"
    author = str(detail.get("creator") or "Автор не указан")[:180]
    title = str(detail.get("title") or "Изображение из открытой коллекции")[:180]
    thumb = API + identifier + "/thumb/?full_size=true"
    return {"id": identifier, "source_id": source["id"], "title": title,
            "author": author, "page_url": page_url, "image_url": thumb,
            "license": {"cc0": "CC0", "pdm": "Public Domain Mark", "by": "CC BY"}[detail["license"]],
            "license_url": license_url, "views": 0, "likes": 0,
            "metrics_available": False}, None


# Получаем изображение после проверки адреса, формата и размера.
def image_bytes(identifier):
    # Обрабатываем ветвь при условии: not ID_PATTERN.fullmatch(str(identifier)).
    if not ID_PATTERN.fullmatch(str(identifier)):
        raise AgentError("OPENVERSE_ID_INVALID")
    return _fetch(API + identifier + "/thumb/?full_size=true", image=True)
