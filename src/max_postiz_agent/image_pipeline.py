# Права источников, локальное ранжирование и публикация исходных изображений.
"""Rights-first image discovery and local CLIP ranking for owner review."""

import hashlib
import io
import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urlsplit

from .config import AgentError, inside, parse_json
from .network import SafeHTTP, validate_url
from .channel_profiles import channel_path, image_config, validate_channel_id


MODEL_SHA = "06e11583385faecfa7273f7cc72b603be915ab9340269482cde69a5d0f5e3350"
TOKENIZER_SHA = "b68d571997a1f81bf521fb73806740ddb91e4ed6666cb6e996c066bb289cf55b"
MODEL_DIR = "data/models/clip-vit-base-patch32-onnx"
SOURCE_FILE = "config/image_sources.json"
RESULT_FILE = "data/image-ranking/latest.json"
AUTO_DIR = "data/image-ranking/auto-runs"
API_HOST = "pixabay.com"
IMAGE_HOSTS = {"pixabay.com", "cdn.pixabay.com"}
APPROVED_LICENSE = {"name": "Pixabay Content License", "url": "https://pixabay.com/service/license-summary/"}
CACHE_DIR = "data/image-ranking/api-cache"
PROMPTS = (
    "high quality imaginative AI generated digital artwork, vivid composition",
    "unrelated ordinary snapshot, text screenshot or low quality image",
)


# Проверяем каталог источников и получаем настройки выбранного канала.
def source_config(root, channel_id=None):
    path = inside(root, SOURCE_FILE)
    # Обрабатываем ветвь при условии: not path.exists().
    if not path.exists():
        path = inside(root, "config/image_sources.example.json")
    data = parse_json(path.read_text(encoding="utf-8-sig"))
    validate_channel_id(data.get("target_channel"))
    # Обрабатываем ветвь при условии: data.get('version') != 2.
    if data.get("version") != 2:
        raise AgentError("IMAGE_CONFIG_INVALID")
    # Последовательно обрабатываем элементы: ('top_n', 'min_views', 'min_likes', 'min_width', 'min_height', 'max_downloads_per_scan').
    for key in ("top_n", "min_views", "min_likes", "min_width", "min_height", "max_downloads_per_scan"):
        # Обрабатываем ветвь при условии: isinstance(data.get(key), bool) or not isinstance(data.get(key), int) or data[key] < 1.
        if isinstance(data.get(key), bool) or not isinstance(data.get(key), int) or data[key] < 1:
            raise AgentError("IMAGE_CONFIG_INVALID")
    # Обрабатываем ветвь при условии: not 1 <= data['top_n'] <= 10 or not 10 <= data['max_downloads_per_scan'] <= 20 or (not 0 <= data.get('min_l….
    if (not 1 <= data["top_n"] <= 10 or not 10 <= data["max_downloads_per_scan"] <= 20
            or not 0 <= data.get("min_local_relevance", -1) <= 1):
        raise AgentError("IMAGE_CONFIG_INVALID")
    # Обрабатываем ветвь при условии: data.get('license') != APPROVED_LICENSE or not data.get('sources').
    if data.get("license") != APPROVED_LICENSE or not data.get("sources"):
        raise AgentError("IMAGE_CONFIG_INVALID")
    ids, sites = set(), set()
    # Последовательно обрабатываем элементы: data['sources'].
    for source in data["sources"]:
        # Обрабатываем ветвь при условии: source.get('id') in ids or not re.fullmatch('[a-z][a-z0-9_]{1,63}', source.get('id', '')).
        if source.get("id") in ids or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", source.get("id", "")):
            raise AgentError("IMAGE_SOURCE_INVALID")
        ids.add(source["id"])
        # Обрабатываем ветвь при условии: source.get('type') == 'pixabay_api'.
        if source.get("type") == "pixabay_api":
            # Обрабатываем ветвь при условии: source.get('endpoint') != 'https://pixabay.com/api/' or source.get('api_key_env') != 'PIXABAY_API_KEY' or s….
            if (source.get("endpoint") != "https://pixabay.com/api/"
                    or source.get("api_key_env") != "PIXABAY_API_KEY"
                    or source.get("rights_evidence") != APPROVED_LICENSE["url"]
                    or not isinstance(source.get("queries"), list) or not source["queries"]
                    or any(not isinstance(q, str) or not q.strip() for q in source["queries"])
                    or len(source["queries"]) > 4 or not 1 <= source.get("per_query", 0) <= 20):
                raise AgentError("IMAGE_SOURCE_INVALID")
            site = "pixabay.com"
        # Обрабатываем ветвь при условии: source.get('type') == 'openverse_source'.
        elif source.get("type") == "openverse_source":
            site = urlsplit(source.get("site_url", "")).hostname
            # Обрабатываем ветвь при условии: not site or urlsplit(source['site_url']).scheme != 'https' or (not re.fullmatch('[A-Za-z_]{2,80}', source.g….
            if (not site or urlsplit(source["site_url"]).scheme != "https"
                    or not re.fullmatch(r"[A-Za-z_]{2,80}", source.get("source_name", ""))
                    or not isinstance(source.get("query"), str) or not source["query"].strip()
                    or len(source["query"]) > 100 or not 1 <= source.get("per_query", 0) <= 10
                    or source.get("rights_evidence") != "https://api.openverse.org/v1/"):
                raise AgentError("IMAGE_SOURCE_INVALID")
        else:
            # Альтернативная ветвь после проверки основного условия.
            raise AgentError("IMAGE_SOURCE_INVALID")
        # Обрабатываем ветвь при условии: site in sites.
        if site in sites:
            raise AgentError("IMAGE_SOURCE_SITE_DUPLICATE")
        sites.add(site)
    return image_config(root, data, channel_id)


# Используем суточный кэш Pixabay и не сохраняем API-ключ в нём.
def pixabay_call(http, root, source, key, **params):
    cache_key = hashlib.sha256(json.dumps({"source": source["id"], "params": params},
                                          sort_keys=True).encode()).hexdigest()
    cache = inside(root, f"{CACHE_DIR}/{cache_key}.json")
    # Обрабатываем ветвь при условии: cache.exists().
    if cache.exists():
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            saved = parse_json(cache.read_text(encoding="utf-8"))
            # Обрабатываем ветвь при условии: datetime.now(timezone.utc) - datetime.fromisoformat(saved['checked_at']) < timedelta(hours=24).
            if datetime.now(timezone.utc) - datetime.fromisoformat(saved["checked_at"]) < timedelta(hours=24):
                return saved["result"]
        # Обрабатываем (ValueError, KeyError, TypeError) и выбираем допустимый исход.
        except (ValueError, KeyError, TypeError):
            pass
    query = urlencode({"key": key, **params})
    status, content, _, final_url = http.get(source["endpoint"] + "?" + query)
    # Обрабатываем ветвь при условии: status != 200 or urlsplit(final_url).hostname != API_HOST.
    if status != 200 or urlsplit(final_url).hostname != API_HOST:
        raise AgentError("IMAGE_SOURCE_RESPONSE_INVALID")
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        data = json.loads(content)
    # Обрабатываем ValueError и выбираем допустимый исход.
    except ValueError as exc:
        raise AgentError("IMAGE_SOURCE_RESPONSE_INVALID") from exc
    # Обрабатываем ветвь при условии: not isinstance(data.get('hits'), list).
    if not isinstance(data.get("hits"), list):
        raise AgentError("IMAGE_SOURCE_RESPONSE_INVALID")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                                 "result": data}, ensure_ascii=False), encoding="utf-8")
    return data


# Получаем поисковые результаты и исключаем повтор идентификаторов.
def candidates(http, root, source, key):
    seen = set()
    # Последовательно обрабатываем элементы: source['queries'].
    for query in source["queries"]:
        result = pixabay_call(http, root, source, key, q=query,
                              image_type=source.get("image_type", "illustration"), safesearch="true", order="popular",
                              per_page=source["per_query"], page=1)
        # Последовательно обрабатываем элементы: result['hits'].
        for item in result["hits"]:
            identifier = str(item.get("id", ""))
            # Обрабатываем ветвь при условии: not identifier.isdigit() or identifier in seen.
            if not identifier.isdigit() or identifier in seen:
                continue
            seen.add(identifier)
            yield item


# Преобразуем счётчик в неотрицательное число либо отмечаем его отсутствие.
def numeric(value):
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        number = int(value)
        return number if number >= 0 else None
    # Обрабатываем (TypeError, ValueError) и выбираем допустимый исход.
    except (TypeError, ValueError):
        return None


# Повторно сверяем автора, лицензию, адреса и доступные счётчики.
def verify_metadata(http, root, source, key, item, cfg):
    identifier = str(item.get("id", ""))
    views = numeric(item.get("views"))
    # Обрабатываем ветвь при условии: views is None or views < cfg['min_views'].
    if views is None or views < cfg["min_views"]:
        return None, "views_missing_or_low"
    likes = numeric(item.get("likes"))
    # Обрабатываем ветвь при условии: likes is None or likes < cfg['min_likes'].
    if likes is None or likes < cfg["min_likes"]:
        return None, "likes_missing_or_low"
    hits = pixabay_call(http, root, source, key, id=identifier).get("hits", [])
    # Обрабатываем ветвь при условии: len(hits) != 1 or str(hits[0].get('id')) != identifier.
    if len(hits) != 1 or str(hits[0].get("id")) != identifier:
        return None, "source_item_missing"
    original = hits[0]
    # Обрабатываем ветвь при условии: numeric(original.get('views')) is None or numeric(original.get('views')) < cfg['min_views'].
    if numeric(original.get("views")) is None or numeric(original.get("views")) < cfg["min_views"]:
        return None, "views_missing_or_low"
    # Обрабатываем ветвь при условии: numeric(original.get('likes')) is None or numeric(original.get('likes')) < cfg['min_likes'].
    if numeric(original.get("likes")) is None or numeric(original.get("likes")) < cfg["min_likes"]:
        return None, "likes_missing_or_low"
    author = original.get("user")
    image_url = original.get("largeImageURL") or original.get("webformatURL", "")
    page_url = original.get("pageURL", "")
    # Обрабатываем ветвь при условии: not author or not image_url.startswith('https://') or urlsplit(image_url).hostname not in IMAGE_HOSTS or (n….
    if (not author or not image_url.startswith("https://")
            or urlsplit(image_url).hostname not in IMAGE_HOSTS
            or not urlsplit(image_url).path.startswith(("/get/", "/photo/"))):
        return None, "metadata_incomplete"
    # Обрабатываем ветвь при условии: urlsplit(page_url).hostname not in {'www.pixabay.com', 'pixabay.com'} or not page_url.startswith('https://').
    if urlsplit(page_url).hostname not in {"www.pixabay.com", "pixabay.com"} or not page_url.startswith("https://"):
        return None, "source_page_invalid"
    # Обрабатываем ветвь при условии: numeric(original.get('imageWidth')) is None or numeric(original.get('imageHeight')) is None.
    if numeric(original.get("imageWidth")) is None or numeric(original.get("imageHeight")) is None:
        return None, "metadata_incomplete"
    return {"source_id": source["id"], "id": identifier, "title": original.get("tags", ""),
            "author": author, "page_url": page_url, "image_url": image_url,
            "license": cfg["license"]["name"], "license_url": cfg["license"]["url"],
            "views": numeric(original["views"]), "likes": numeric(original["likes"]),
            "rights_evidence": source["rights_evidence"], "modified": False}, None


# Получаем изображение после проверки адреса, формата и размера.
def image_bytes(url, max_bytes, reader_factory=SafeHTTP):
    validate_url(url, resolve=False)
    # Обрабатываем ветвь при условии: urlsplit(url).hostname not in IMAGE_HOSTS.
    if urlsplit(url).hostname not in IMAGE_HOSTS:
        raise AgentError("IMAGE_HOST_INVALID")
    reader = reader_factory(max_bytes=max_bytes, redirects=2)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        status, content, headers, final_url = reader.get(url)
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        reader.close()
    # Обрабатываем ветвь при условии: status != 200 or urlsplit(final_url).hostname not in IMAGE_HOSTS.
    if status != 200 or urlsplit(final_url).hostname not in IMAGE_HOSTS:
        raise AgentError("IMAGE_HOST_INVALID")
    # Обрабатываем ветвь при условии: headers.get('content-type', '').split(';')[0].lower() not in {'image/jpeg', 'image/png'}.
    if headers.get("content-type", "").split(";")[0].lower() not in {"image/jpeg", "image/png"}:
        raise AgentError("IMAGE_FORMAT_INVALID")
    return content


# Проверяем изображение и готовим пиксели для локальной модели.
def decode_image(content, cfg):
    from PIL import Image, ImageOps, UnidentifiedImageError

    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with Image.open(io.BytesIO(content)) as opened:
            # Обрабатываем ветвь при условии: opened.format not in {'JPEG', 'PNG'} or opened.width * opened.height > 40000000.
            if opened.format not in {"JPEG", "PNG"} or opened.width * opened.height > 40_000_000:
                raise AgentError("IMAGE_FORMAT_INVALID")
            # Обрабатываем ветвь при условии: opened.width < cfg['min_width'] or opened.height < cfg['min_height'].
            if opened.width < cfg["min_width"] or opened.height < cfg["min_height"]:
                raise AgentError("IMAGE_TOO_SMALL")
            layer = ImageOps.exif_transpose(opened).convert("RGBA")
            matte = Image.new("RGBA", layer.size, "#f3f5f7")
            matte.alpha_composite(layer)
            picture = matte.convert("RGB")
    # Обрабатываем (OSError, UnidentifiedImageError) и выбираем допустимый исход.
    except (OSError, UnidentifiedImageError) as exc:
        raise AgentError("IMAGE_FORMAT_INVALID") from exc
    return picture


# Вычисляем компактный признак визуального сходства.
def perceptual_hash(image):
    from PIL import Image

    values = list(image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).get_flattened_data())
    bits = sum((1 << (row * 8 + col)) for row in range(8) for col in range(8)
               if values[row * 9 + col] > values[row * 9 + col + 1])
    return f"{bits:016x}"


# Локальная ONNX-модель тематической оценки изображений.
class LocalCLIP:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, root, prompts=PROMPTS):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        # Обрабатываем ImportError и выбираем допустимый исход.
        except ImportError as exc:
            raise AgentError("LOCAL_IMAGE_MODEL_DEPENDENCY_REQUIRED") from exc
        self.path = inside(root, MODEL_DIR)
        model = self.path / "onnx/model_quantized.onnx"
        tokenizer = self.path / "tokenizer.json"
        # Обрабатываем ветвь при условии: not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA or (not tokenizer.exists(….
        if (not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA
                or not tokenizer.exists() or hashlib.sha256(tokenizer.read_bytes()).hexdigest() != TOKENIZER_SHA):
            raise AgentError("LOCAL_IMAGE_MODEL_REQUIRED")
        self.runtime = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(tokenizer))
        self.model_sha256 = MODEL_SHA
        encoded = [self.tokenizer.encode(label).ids[:77] for label in prompts]
        self.length = max(map(len, encoded))
        import numpy as np

        self.ids = np.array([part + [0] * (self.length - len(part)) for part in encoded], dtype=np.int64)
        self.mask = np.array([[1] * len(part) + [0] * (self.length - len(part)) for part in encoded], dtype=np.int64)

    # Вычисляем тематическую оценку изображения локально.
    def score(self, image):
        import numpy as np
        from PIL import Image, ImageOps

        resized = ImageOps.fit(image, (224, 224), method=Image.Resampling.BICUBIC)
        pixels = np.asarray(resized, dtype=np.float32) / 255.0
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        pixels = np.transpose((pixels - mean) / std, (2, 0, 1))[None]
        logits = self.runtime.run(["logits_per_image"], {
            "input_ids": self.ids, "attention_mask": self.mask,
            "pixel_values": pixels,
        })[0][0]
        values = np.exp(logits - np.max(logits))
        score = float(values[0] / values.sum())
        # Обрабатываем ветвь при условии: not math.isfinite(score).
        if not math.isfinite(score):
            raise AgentError("LOCAL_IMAGE_MODEL_FAILED")
        return round(score, 5)


# Объединяем доступные показатели популярности и тематическую оценку.
def rank_record(record):
    relevance = record["local_relevance"]
    # Обрабатываем ветвь при условии: record.get('metrics_available', True).
    if record.get("metrics_available", True):
        return (record["views"] * record["likes"]) ** 0.5 * (0.5 + relevance)
    return 1000 * relevance


# Выбираем верхние позиции, исключая URL, хеши и визуальные повторы.
def choose(records, limit=10):
    chosen, seen_urls, seen_sha, seen_phash = [], set(), set(), []
    # Последовательно обрабатываем элементы: sorted(records, key=rank_record, reverse=True).
    for record in sorted(records, key=rank_record, reverse=True):
        phash = int(record["phash"], 16)
        # Обрабатываем ветвь при условии: record['page_url'] in seen_urls or record['sha256'] in seen_sha or any(((phash ^ previous).bit_count() <= 6….
        if (record["page_url"] in seen_urls or record["sha256"] in seen_sha
                or any((phash ^ previous).bit_count() <= 6 for previous in seen_phash)):
            continue
        chosen.append(record)
        seen_urls.add(record["page_url"])
        seen_sha.add(record["sha256"])
        seen_phash.append(phash)
        # Обрабатываем ветвь при условии: len(chosen) == limit.
        if len(chosen) == limit:
            break
    return chosen


# Исключаем изображения, уже подготовленные для этого канала.
def used_images(root, channel_id="test"):
    """Exclude every previously staged image, including unresolved deliveries."""
    from sqlalchemy import select
    from .config import Settings
    from .db import Database, Draft

    db = Database(Settings.load(root))
    db.initialize()
    used = {"ids": set(), "pages": set(), "hashes": set()}
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with db.session() as session:
            # Последовательно обрабатываем элементы: session.scalars(select(Draft).where(Draft.channel_id == channel_id)).
            for draft in session.scalars(select(Draft).where(Draft.channel_id == channel_id)):
                image = draft.current.get("image", {})
                # Обрабатываем ветвь при условии: image.
                if image:
                    used["ids"].add((image.get("source_id"), str(image.get("source_image_id"))))
                    used["pages"].add(image.get("page_url"))
                    used["hashes"].add(image.get("source_sha256"))
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        db.engine.dispose()
    return used


# Собираем разрешённых кандидатов и ранжируем их локальной моделью.
def scan(root, cfg=None, http=None, scorer=None, *, all_sources=False, exclude_used=False, channel_id=None):
    cfg = cfg or source_config(root, channel_id)
    channel_id = cfg["target_channel"]
    output = inside(root, "data/image-ranking/" + channel_path(channel_id, "latest.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    enabled = [source for source in cfg["sources"] if source.get("enabled")]
    missing = [source["api_key_env"] for source in enabled
               if source["type"] == "pixabay_api" and not os.getenv(source["api_key_env"])]
    # Обрабатываем ветвь при условии: missing.
    if missing:
        report = {"state": "needs_source_key", "missing_env_names": sorted(set(missing)),
                  "top": [], "rejected": {}, "checked_at": datetime.now(timezone.utc).isoformat()}
        report.update(channel_id=channel_id, profile_hash=cfg.get("profile_hash"), holiday_names=cfg.get("holiday_names", []))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    scorer = scorer or LocalCLIP(root, cfg.get("local_prompts", PROMPTS))
    own_http = http is None
    http = http or SafeHTTP(max_bytes=5_242_880, redirects=2)
    accepted, rejected, fetched, downloaded, scanned_sources = [], {}, 0, 0, []
    used = used_images(root, channel_id) if exclude_used else {"ids": set(), "pages": set(), "hashes": set()}
    source_results = {source["id"]: {"state": "not_scanned", "candidates": 0, "examined": 0,
                                   "downloaded": 0, "eligible": 0, "rejected": {}}
                      for source in enabled}
    source_result = None

    # Отклоняем материал и прекращаем его продвижение к отправке.
    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1
        source_result["rejected"][reason] = source_result["rejected"].get(reason, 0) + 1

    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        from . import openverse_source

        openverse = [source for source in enabled if source["type"] == "openverse_source"]
        slot = datetime.now(timezone(timedelta(hours=3))).hour // 4
        scheduled = {source["id"] for index, source in enumerate(openverse) if index % 6 == slot}
        download_limit = (cfg["max_downloads_per_scan"] + 2 * len(openverse)
                          if all_sources else cfg["max_downloads_per_scan"])
        # Последовательно обрабатываем элементы: sorted(enabled, key=lambda item: item['type'] == 'pixabay_api').
        for source in sorted(enabled, key=lambda item: item["type"] == "pixabay_api"):
            source_result = source_results[source["id"]]
            # Обрабатываем ветвь при условии: not all_sources and source['type'] == 'openverse_source' and (source['id'] not in scheduled).
            if not all_sources and source["type"] == "openverse_source" and source["id"] not in scheduled:
                source_result["state"] = "not_scheduled"
                continue
            scanned_sources.append(source["id"])
            source_result["state"] = "scanning"
            # Обрабатываем ветвь при условии: source['type'] == 'pixabay_api'.
            if source["type"] == "pixabay_api":
                items = list(candidates(http, root, source, os.environ[source["api_key_env"]]))
                items.sort(key=lambda row: (numeric(row.get("views")) or 0) *
                           (numeric(row.get("likes")) or 0), reverse=True)
                source_budget = cfg["max_downloads_per_scan"]
            else:
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    items = openverse_source.candidates(root, source)
                # Обрабатываем AgentError и выбираем допустимый исход.
                except AgentError as exc:
                    # Обрабатываем ветвь при условии: exc.code in {'SOURCE_HTTP_403', 'SOURCE_HTTP_429'}.
                    if exc.code in {"SOURCE_HTTP_403", "SOURCE_HTTP_429"}:
                        raise
                    reject(exc.code)
                    source_result.update(state="error", code=exc.code)
                    continue
                source_budget = 2
            source_result["candidates"] = len(items)
            source_downloaded = 0
            # Последовательно обрабатываем элементы: items.
            for item in items:
                # Обрабатываем ветвь при условии: downloaded >= download_limit or source_downloaded >= source_budget.
                if downloaded >= download_limit or source_downloaded >= source_budget:
                    break
                fetched += 1
                source_result["examined"] += 1
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    # Обрабатываем ветвь при условии: (source['id'], str(item.get('id'))) in used['ids'].
                    if (source["id"], str(item.get("id"))) in used["ids"]:
                        reject("previously_used")
                        continue
                    # Обрабатываем ветвь при условии: source['type'] == 'pixabay_api'.
                    if source["type"] == "pixabay_api":
                        record, reason = verify_metadata(http, root, source,
                                                         os.environ[source["api_key_env"]], item, cfg)
                    else:
                        # Альтернативная ветвь после проверки основного условия.
                        record, reason = openverse_source.verify(root, source, item)
                    # Обрабатываем ветвь при условии: reason.
                    if reason:
                        reject(reason)
                        continue
                    # Обрабатываем ветвь при условии: record['page_url'] in used['pages'].
                    if record["page_url"] in used["pages"]:
                        reject("previously_used")
                        continue
                    downloaded += 1
                    source_downloaded += 1
                    source_result["downloaded"] += 1
                    content = (image_bytes(record["image_url"], 10_485_760)
                               if source["type"] == "pixabay_api"
                               else openverse_source.image_bytes(record["id"]))
                    picture = decode_image(content, cfg)
                    # Обрабатываем ветвь при условии: hashlib.sha256(content).hexdigest() in used['hashes'].
                    if hashlib.sha256(content).hexdigest() in used["hashes"]:
                        reject("previously_used")
                        continue
                    record.update(sha256=hashlib.sha256(content).hexdigest(), phash=perceptual_hash(picture),
                                  width=picture.width, height=picture.height,
                                  local_relevance=scorer.score(picture), model_sha256=getattr(scorer, "model_sha256", "test"),
                                  rights_checked_at=datetime.now(timezone.utc).isoformat())
                    # Обрабатываем ветвь при условии: record['local_relevance'] < cfg['min_local_relevance'].
                    if record["local_relevance"] < cfg["min_local_relevance"]:
                        reject("local_relevance_low")
                        continue
                    suffix = ".jpg" if content[:3] == b"\xff\xd8\xff" else ".png"
                    destination = inside(root, "data/image-ranking/files/" + record["sha256"] + suffix)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # Обрабатываем ветвь при условии: not destination.exists().
                    if not destination.exists():
                        destination.write_bytes(content)
                    record["path"] = destination.relative_to(root).as_posix()
                    accepted.append(record)
                    source_result["eligible"] += 1
                # Обрабатываем AgentError и выбираем допустимый исход.
                except AgentError as exc:
                    # Обрабатываем ветвь при условии: exc.code in {'SOURCE_HTTP_403', 'SOURCE_HTTP_429'}.
                    if exc.code in {"SOURCE_HTTP_403", "SOURCE_HTTP_429"}:
                        raise
                    reject(exc.code)
            source_result["state"] = ("no_results" if not items else
                                      "accepted" if source_result["eligible"] else "no_eligible_images")
        top = choose(accepted, cfg["top_n"])
        # Последовательно обрабатываем элементы: enumerate(top, 1).
        for index, row in enumerate(top, 1):
            row["rank"] = index
            row["rank_score"] = round(rank_record(row), 3)
        report = {"state": "ready" if len(top) == cfg["top_n"] else "insufficient_candidates",
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "examined": fetched, "downloaded": downloaded, "eligible": len(accepted), "top": top,
                  "rejected": rejected, "source_ids": scanned_sources,
                  "configured_source_count": len(enabled), "source_results": source_results,
                  "all_sources": all_sources, "exclude_used": exclude_used,
                  "download_limit": download_limit}
        report.update(channel_id=channel_id, profile_hash=cfg.get("profile_hash"), holiday_names=cfg.get("holiday_names", []))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        # Обрабатываем ветвь при условии: source_result.
        if source_result:
            source_result.update(state="error", code=exc.code)
        report = {"state": "source_error", "code": exc.code,
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "examined": fetched, "eligible_before_error": len(accepted),
                  "top": [], "rejected": rejected, "source_ids": scanned_sources,
                  "configured_source_count": len(enabled), "source_results": source_results,
                  "accepted_before_error": accepted, "all_sources": all_sources,
                  "exclude_used": exclude_used}
        report.update(channel_id=channel_id, profile_hash=cfg.get("profile_hash"), holiday_names=cfg.get("holiday_names", []))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        # Обрабатываем ветвь при условии: own_http.
        if own_http:
            http.close()


# Читаем последнюю подборку именно выбранного канала.
def latest(root, channel_id="test"):
    path = inside(root, "data/image-ranking/" + channel_path(channel_id, "latest.json"))
    return parse_json(path.read_text(encoding="utf-8")) if path.exists() else {"state": "not_scanned", "top": []}


# Сверяем тему, свежесть подборки и хеш выбранного файла.
def selected(root, rank, sha256, channel_id="test"):
    from .media import inspect_image

    report = latest(root, channel_id)
    cfg = source_config(root, channel_id)
    # Обрабатываем ветвь при условии: report.get('profile_hash') and report['profile_hash'] != cfg['profile_hash'] or (cfg['profile_explicit'] an….
    if (report.get("profile_hash") and report["profile_hash"] != cfg["profile_hash"]) or (cfg["profile_explicit"] and not report.get("profile_hash")):
        raise AgentError("IMAGE_TOPIC_CHANGED")
    # Обрабатываем ветвь при условии: report.get('state') != 'ready'.
    if report.get("state") != "ready":
        raise AgentError("IMAGE_TOP_NOT_READY")
    checked = datetime.fromisoformat(report["checked_at"])
    # Обрабатываем ветвь при условии: (datetime.now(timezone.utc) - checked).total_seconds() > 86400.
    if (datetime.now(timezone.utc) - checked).total_seconds() > 86400:
        raise AgentError("IMAGE_TOP_EXPIRED")
    record = next((row for row in report["top"] if row["rank"] == rank), None)
    # Обрабатываем ветвь при условии: not record or record['sha256'] != sha256.
    if not record or record["sha256"] != sha256:
        raise AgentError("IMAGE_SELECTION_CHANGED")
    # Обрабатываем ветвь при условии: inspect_image(root, record['path'])['sha256'] != sha256.
    if inspect_image(root, record["path"])["sha256"] != sha256:
        raise AgentError("IMAGE_HASH_CHANGED")
    return record


# Формируем подпись с автором, источником и лицензией.
def publication_body(record, title="Иллюстрация из открытой коллекции"):
    author = re.sub(r"[\r\n\t]+", " ", str(record["author"]))[:180].strip()
    metrics = (f"Просмотры источника: {record['views']:,}. Лайки: {record['likes']:,}."
            if record.get("metrics_available", True) else
            "У источника открытых счётчиков просмотров и лайков нет; оценка выполнена локальной моделью.")
    return (f"{title}\n\n"
            f"Автор: {author}\n"
            f"Источник: {record['page_url']}\n"
            f"Лицензия: {record['license']} — {record['license_url']}\n"
            + metrics)


# Используем исходные байты изображения без добавления плашек.
def publication_image(root, record):
    """Keep the original pixels and aspect ratio; metadata belongs in the caption."""
    from .media import inspect_image

    info = inspect_image(root, record["path"])
    # Обрабатываем ветвь при условии: info['sha256'] != record['sha256'].
    if info["sha256"] != record["sha256"]:
        raise AgentError("IMAGE_HASH_CHANGED")
    return {"path": record["path"], "sha256": info["sha256"]}


# Проверяем права, файл и адресат; согласуем и отправляем один материал.
def publish_rank(service, rank, sha256, *, channel_id="test"):
    """Explicit owner action; one approved image, paused channel, one durable outbox intent."""
    from sqlalchemy import select

    from .config import fingerprint
    from .db import Channel, Draft, DraftRevision, Intent, Story, now
    from .editorial import validate_draft

    root = service.settings.root
    # Изменённые редакционные настройки требуют активации до подготовки намерения отправки.
    if service.refresh():
        raise AgentError(service.config_error)
    cfg = source_config(root, channel_id)
    record = selected(root, rank, sha256, channel_id)
    # Обрабатываем ветвь при условии: service.publisher.mode != 'browser'.
    if service.publisher.mode != "browser":
        raise AgentError("IMAGE_BROWSER_TEST_REQUIRED")
    profile = service.settings.channel(channel_id)
    # Обрабатываем ветвь при условии: profile['publication']['mode'] != 'review'.
    if profile["publication"]["mode"] != "review":
        raise AgentError("IMAGE_REVIEW_MODE_REQUIRED")
    source = next((s for s in cfg["sources"] if s["id"] == record["source_id"] and s["enabled"]), None)
    # Обрабатываем ветвь при условии: not source.
    if not source:
        raise AgentError("IMAGE_SOURCE_DISABLED")
    # Обрабатываем ветвь при условии: source['type'] == 'pixabay_api'.
    if source["type"] == "pixabay_api":
        key = os.getenv(source["api_key_env"])
        # Обрабатываем ветвь при условии: not key.
        if not key:
            raise AgentError("IMAGE_SOURCE_KEY_REQUIRED")
        item = {"id": record["id"], "views": record["views"], "likes": record["likes"]}
        client = SafeHTTP(max_bytes=5_242_880, redirects=2)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            current, reason = verify_metadata(client, root, source, key, item, cfg)
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            client.close()
        current_image = image_bytes(record["image_url"], 10_485_760)
    else:
        # Альтернативная ветвь после проверки основного условия.
        from . import openverse_source

        current, reason = openverse_source.verify(root, source, {"id": record["id"]})
        current_image = openverse_source.image_bytes(record["id"])
    # Обрабатываем ветвь при условии: reason or current['page_url'] != record['page_url'] or current['license_url'] != record['license_url'] or (….
    if (reason or current["page_url"] != record["page_url"]
            or current["license_url"] != record["license_url"]
            or current["author"] != record["author"]
            or current["image_url"] != record["image_url"]):
        raise AgentError("IMAGE_RIGHTS_CHANGED")
    # Обрабатываем ветвь при условии: hashlib.sha256(current_image).hexdigest() != sha256.
    if hashlib.sha256(current_image).hexdigest() != sha256:
        raise AgentError("IMAGE_SOURCE_FILE_CHANGED")
    image = publication_image(root, record)
    body = publication_body(record, cfg["caption_title"])
    values = {"title": cfg["caption_title"],
              "body": body, "language": "ru", "source_ids": [], "decision": "review",
              "reason_codes": ["OWNER_SELECTED_LICENSED_IMAGE"], "claims": [],
              "confidence": 1.0, "payload": {"origin": "licensed_image", "source_id": record["source_id"]}}
    values["checks"] = validate_draft(values, [], profile, allow_original=True)
    values["image"] = {"path": image["path"], "sha256": image["sha256"],
                       "source_sha256": record["sha256"],
                       "source_id": record["source_id"], "source_image_id": record["id"],
                       "page_url": record["page_url"], "license": record["license"],
                       "license_url": record["license_url"], "author": record["author"]}
    story_key = fingerprint({"channel_id": channel_id, "image_source": record["source_id"], "image_id": record["id"],
                             "sha256": record["sha256"]})
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as session:
        channel = session.get(Channel, channel_id)
        # Обрабатываем ветвь при условии: not channel or not channel.paused or (not channel.chat_id) or (not channel.capabilities.get('publish')).
        if not channel or not channel.paused or not channel.chat_id or not channel.capabilities.get("publish"):
            raise AgentError("IMAGE_CHANNEL_NOT_READY")
        # Обрабатываем ветвь при условии: session.scalar(select(Story).where(Story.text_hash == story_key)).
        if session.scalar(select(Story).where(Story.text_hash == story_key)):
            raise AgentError("IMAGE_ALREADY_STAGED")
        # Последовательно обрабатываем элементы: session.scalars(select(Draft).where(Draft.channel_id == channel_id)).
        for other in session.scalars(select(Draft).where(Draft.channel_id == channel_id)):
            # Обрабатываем ветвь при условии: other.state == 'scheduled'.
            if other.state == "scheduled":
                raise AgentError("IMAGE_OTHER_POST_SCHEDULED")
            prior = other.current.get("image", {})
            # Обрабатываем ветвь при условии: other.state == 'published' and prior and (prior.get('source_sha256') == sha256 or prior.get('page_url') == ….
            if (other.state == "published" and prior and
                    (prior.get("source_sha256") == sha256 or prior.get("page_url") == record["page_url"])):
                raise AgentError("IMAGE_ALREADY_PUBLISHED")
        story = Story(text_hash=story_key, title=values["title"], text=body)
        session.add(story)
        session.flush()
        draft = Draft(channel_id=channel_id, story_id=story.id, current=values,
                      config_id=service.settings.hash, state="review_pending")
        session.add(draft)
        session.flush()
        draft_id = draft.id
        session.add(DraftRevision(draft_id=draft_id, revision=1, payload=values,
                                  config_id=service.settings.hash))
        service.db.audit(session, "owner", "image_stage", draft_id, sha256=sha256)
    service.approve(draft_id, 1, fingerprint(values), actor="owner")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as session:
        session.get(Draft, draft_id).scheduled_at = now() - 1
        service.db.audit(session, "owner", "image_publish_now", draft_id)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        service.pause(channel_id, False, actor="owner")
        service.publish_due()
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        service.pause(channel_id, True, actor="owner")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        intent = session.scalar(select(Intent).where(Intent.draft_id == draft_id))
        return {"draft_id": draft_id, "state": intent.state if intent else "awaiting_intent",
                "intent_id": intent.id if intent else None}


# Выбираем одно новое изображение и фиксируем попытку до отправки.
def auto_run(service, *, dry_run=False, at=None, scanner=None, publisher=publish_rank, channel_id="test"):
    """One conservative scheduled selection per four-hour Moscow time slot."""
    from sqlalchemy import select

    from .config import fingerprint
    from .db import Channel, Draft, Intent

    root = service.settings.root
    from .channel_profiles import window

    clock, slot_start, _, slot, _ = window(root, channel_id, at)
    checkpoint = inside(root, "data/image-ranking/" + channel_path(channel_id, f"auto-runs/{slot}.json"))
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    # Обрабатываем ветвь при условии: checkpoint.exists() and (not dry_run).
    if checkpoint.exists() and not dry_run:
        return {"state": "already_attempted", "slot": slot}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        channel = session.get(Channel, channel_id)
        # Обрабатываем ветвь при условии: not channel or not channel.paused or (not channel.chat_id) or (not channel.capabilities.get('publish')).
        if (not channel or not channel.paused or not channel.chat_id
                or not channel.capabilities.get("publish")):
            return {"state": "channel_not_ready", "slot": slot}
        chat_id = channel.chat_id
        drafts = list(session.scalars(select(Draft).where(Draft.channel_id == channel_id)))
        prior = [(draft, session.scalar(select(Intent).where(Intent.draft_id == draft.id)))
                 for draft in drafts if draft.current.get("image")]
        # Обрабатываем ветвь при условии: any((intent and intent.state in {'pending', 'sending', 'unknown'} for _, intent in prior)).
        if any(intent and intent.state in {"pending", "sending", "unknown"} for _, intent in prior):
            return {"state": "unresolved_publication", "slot": slot}
        # Обрабатываем ветвь при условии: any((intent and intent.published_at and (intent.published_at >= slot_start.timestamp()) for _, intent in pr….
        if any(intent and intent.published_at and intent.published_at >= slot_start.timestamp()
               for _, intent in prior):
            return {"state": "already_published_in_slot", "slot": slot}
        seen = {(draft.current["image"].get("source_id"),
                 str(draft.current["image"].get("source_image_id"))) for draft, _ in prior}
        seen_pages = {draft.current["image"].get("page_url") for draft, _ in prior}
    # Обрабатываем ветвь при условии: service.publisher.mode != 'browser'.
    if service.publisher.mode != "browser":
        return {"state": "browser_required", "slot": slot}
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        inspection = service.publisher.inspect(chat_id)
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        return {"state": "channel_inspection_failed", "slot": slot, "code": exc.code}
    # Обрабатываем ветвь при условии: not inspection.get('publish') or inspection.get('binding_hash') != fingerprint(service.settings.app['max']).
    if not inspection.get("publish") or inspection.get("binding_hash") != fingerprint(service.settings.app["max"]):
        return {"state": "channel_not_ready", "slot": slot}
    # Обрабатываем ветвь при условии: not dry_run.
    if not dry_run:
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with checkpoint.open("x", encoding="utf-8") as output:
                json.dump({"state": "started", "slot": slot}, output)
        # Обрабатываем FileExistsError и выбираем допустимый исход.
        except FileExistsError:
            return {"state": "already_attempted", "slot": slot}
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        from .holidays import campaign, search_config
        holiday = campaign(root, channel_id, clock, reserve=not dry_run, slot=slot)
        options = {"exclude_used": True, **({"channel_id": channel_id} if channel_id != "test" else {})}
        if holiday and holiday["holiday_names"]:
            options["cfg"] = search_config(root, source_config(root, channel_id), holiday)
        report = scanner(root) if scanner else scan(root, **options)
        if report["state"] != "ready":
            result = {"state": "scan_not_ready", "slot": slot, "scan_state": report["state"]}
            # Отсутствие подходящих праздничных картинок не блокирует обычные следующие интервалы.
            if holiday and holiday["holiday_names"] and report["state"] in {"insufficient_candidates", "no_new_image"}:
                result["state"] = "no_new_image"
        else:
            # Альтернативная ветвь после проверки основного условия.
            row = next((item for item in report["top"]
                        if (item["source_id"], str(item["id"])) not in seen
                        and item["page_url"] not in seen_pages), None)
            # Обрабатываем ветвь при условии: not row.
            if not row:
                result = {"state": "no_new_image", "slot": slot}
            # Обрабатываем ветвь при условии: dry_run.
            elif dry_run:
                result = {"state": "dry_run_ready", "slot": slot, "rank": row["rank"],
                          "sha256": row["sha256"], "page_url": row["page_url"]}
            else:
                # Альтернативная ветвь после проверки основного условия.
                result = {**publisher(service, row["rank"], row["sha256"], **({"channel_id": channel_id} if channel_id != "test" else {})),
                          "slot": slot, "rank": row["rank"], "source_image_id": row["id"]}
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        result = {"state": "error", "slot": slot, "code": exc.code}
    # Обрабатываем ветвь при условии: not dry_run.
    if "holiday" in locals() and holiday:
        result["holiday_search"] = holiday
    if not dry_run:
        checkpoint.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
