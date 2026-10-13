import hashlib
import io
import json
from datetime import datetime, timezone, timedelta

import pytest
from PIL import Image

from max_postiz_agent import image_pipeline as images
from max_postiz_agent import openverse_source
from max_postiz_agent.cli import main
from max_postiz_agent.config import AgentError
from max_postiz_agent.media import inspect_image
from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Channel, Draft, Intent
from test_browser_publishing import browser_config


# Накладываем настройки одного канала на общий каталог источников.
def image_config(project):
    config = json.loads((images_path() / "config/image_sources.example.json").read_text(encoding="utf-8"))
    # Последовательно обрабатываем элементы: config['sources'].
    for source in config["sources"]:
        # Обрабатываем ветвь при условии: source['type'] == 'openverse_source'.
        if source["type"] == "openverse_source":
            source["enabled"] = False
    (project / "config/image_sources.json").write_text(json.dumps(config), encoding="utf-8")
    return config


# Операция images_path: данного модуля.
def images_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


# Проверяем сценарий: no source key does not invent a top.
def test_no_source_key_does_not_invent_a_top(project, monkeypatch, capsys):
    image_config(project)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    assert main(["--root", str(project), "images", "scan"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "needs_source_key"
    assert report["top"] == [] and report["missing_env_names"] == ["PIXABAY_API_KEY"]
    assert main(["--root", str(project), "images", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["top_count"] == 0 and status["sources"][0]["key_available"] is False


# Проверяем сценарий: rights and real metrics are required.
def test_rights_and_real_metrics_are_required(project, monkeypatch):
    cfg = image_config(project)
    source = cfg["sources"][0]
    item = {"id": 123, "views": 10001, "likes": 101}
    detail = {**item, "user": "Author", "tags": "art, AI",
              "pageURL": "https://pixabay.com/illustrations/art-123/",
              "largeImageURL": "https://pixabay.com/get/art-123.jpg",
              "imageWidth": 1024, "imageHeight": 1024}
    monkeypatch.setattr(images, "pixabay_call", lambda *a, **k: {"hits": [detail]})
    record, reason = images.verify_metadata(None, project, source, "key", item, cfg)
    assert reason is None and record["views"] == 10001 and record["likes"] == 101
    # Последовательно обрабатываем элементы: [({'views': None}, 'views_missing_or_low'), ({'likes': None}, 'likes_missing_or_low')].
    for changed, expected in [({"views": None}, "views_missing_or_low"),
                              ({"likes": None}, "likes_missing_or_low")]:
        assert images.verify_metadata(None, project, source, "key", {**item, **changed}, cfg)[1] == expected
    monkeypatch.setattr(images, "pixabay_call", lambda *a, **k: {"hits": []})
    assert images.verify_metadata(None, project, source, "key", item, cfg)[1] == "source_item_missing"


# Проверяем сценарий: pixabay api uses 24 hour cache without storing key.
def test_pixabay_api_uses_24_hour_cache_without_storing_key(project):
    source = image_config(project)["sources"][0]
    calls = []
    # Структура HTTP объединяет связанные данные и операции модуля.
    class HTTP:
        # Получаем ответ с учётом сетевых и размерных ограничений.
        def get(self, url):
            calls.append(url)
            return 200, b'{"hits":[]}', {}, "https://pixabay.com/api/"
    first = images.pixabay_call(HTTP(), project, source, "private-test-key", q="digital art")
    second = images.pixabay_call(HTTP(), project, source, "private-test-key", q="digital art")
    assert first == second == {"hits": []}
    assert len(calls) == 1
    assert "private-test-key" in calls[0]
    cache = list((project / images.CACHE_DIR).glob("*.json"))
    assert len(cache) == 1 and "private-test-key" not in cache[0].read_text()


# Проверяем сценарий: visual and binary duplicates are removed.
def test_visual_and_binary_duplicates_are_removed():
    # Операция row: данного модуля.
    def row(index, phash, sha):
        return {"views": 1000 - index, "likes": 100, "local_relevance": .8,
                "phash": phash, "sha256": sha, "page_url": f"https://example.org/{index}"}

    records = [row(1, "0000000000000000", "a"), row(2, "0000000000000001", "b"),
               row(3, "ffffffffffffffff", "c"), row(4, "aaaaaaaaaaaaaaaa", "c")]
    assert [x["sha256"] for x in images.choose(records)] == ["a", "c"]


# Проверяем сценарий: local image validation rejects corruption.
def test_local_image_validation_rejects_corruption(project):
    path = project / "data/image-ranking/files/example.png"
    path.parent.mkdir(parents=True)
    picture = Image.new("RGB", (512, 512), "navy")
    picture.save(path)
    assert inspect_image(project, "data/image-ranking/files/example.png")["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b"not an image")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="IMAGE_FORMAT_INVALID"):
        inspect_image(project, "data/image-ranking/files/example.png")


# Проверяем сценарий: scan ranks and filters distinct files.
def test_scan_ranks_and_filters_distinct_files(project, monkeypatch):
    cfg = image_config(project)
    cfg["top_n"] = 2
    cfg["min_local_relevance"] = 0.5
    cfg["sources"][0]["queries"] = ["one"]
    monkeypatch.setenv("PIXABAY_API_KEY", "unused-in-mock")
    items = [{"id": str(i), "views": 20000 + i, "likes": 101,
              "largeImageURL": f"https://pixabay.com/get/{i}.png"} for i in range(3)]
    monkeypatch.setattr(images, "candidates", lambda *a: iter(items))
    monkeypatch.setattr(images, "verify_metadata", lambda http, root, source, key, item, cfg: (
        {"source_id": source["id"], "id": item["id"], "title": "art", "author": "Author",
         "page_url": f"https://pixabay.com/illustrations/a-{item['id']}/",
         "image_url": item["largeImageURL"], "license": "Pixabay Content License",
         "license_url": "https://pixabay.com/service/license-summary/",
         "views": int(item["views"]), "likes": 101, "rights_evidence": "evidence",
         "modified": False}, None))

    # Операция fake_bytes: данного модуля.
    def fake_bytes(url, max_bytes):
        index = int(url.rsplit("/", 1)[1].split(".")[0])
        picture = Image.new("RGB", (512, 512), "white")
        # Последовательно обрабатываем элементы: range(index * 40, index * 40 + 80).
        for x in range(index * 40, index * 40 + 80):
            # Последовательно обрабатываем элементы: range(512).
            for y in range(512):
                picture.putpixel((x, y), (0, 0, 0))
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        return buffer.getvalue()

    monkeypatch.setattr(images, "image_bytes", fake_bytes)
    # Структура Scorer объединяет связанные данные и операции модуля.
    class Scorer:
        model_sha256 = "local-test"
        # Вычисляем тематическую оценку изображения локально.
        def score(self, picture):
            return .75
    # Структура HTTP объединяет связанные данные и операции модуля.
    class HTTP:
        pass
    report = images.scan(project, cfg=cfg, http=HTTP(), scorer=Scorer())
    assert report["state"] == "ready"
    assert len(report["top"]) == 2
    assert len({row["sha256"] for row in report["top"]}) == 2
    assert all(row["model_sha256"] == "local-test" for row in report["top"])


# Проверяем сценарий: scan fills top when openverse group has no results.
def test_scan_fills_top_when_openverse_group_has_no_results(project, monkeypatch):
    cfg = image_config(project)
    cfg["sources"][1]["enabled"] = True
    monkeypatch.setenv("PIXABAY_API_KEY", "unused-in-mock")
    items = [{"id": str(i), "views": 20000 + i, "likes": 101} for i in range(12)]
    monkeypatch.setattr(images, "candidates", lambda *a: iter(items))
    monkeypatch.setattr(openverse_source, "candidates", lambda *a: [])
    monkeypatch.setattr(images, "verify_metadata", lambda http, root, source, key, item, cfg: (
        {"source_id": source["id"], "id": item["id"], "title": "art", "author": "Author",
         "page_url": f"https://pixabay.com/illustrations/a-{item['id']}/",
         "image_url": f"https://pixabay.com/get/{item['id']}.png",
         "license": "Pixabay Content License",
         "license_url": "https://pixabay.com/service/license-summary/",
         "views": item["views"], "likes": item["likes"]}, None))
    monkeypatch.setattr(images, "image_bytes", lambda url, limit: url.encode())
    monkeypatch.setattr(images, "decode_image", lambda content, cfg: Image.new("RGB", (512, 512)))
    monkeypatch.setattr(images, "perceptual_hash", lambda picture: "0000000000000000")
    monkeypatch.setattr(images, "choose", lambda records, limit: records[:limit])
    # Структура Scorer объединяет связанные данные и операции модуля.
    class Scorer:
        calls = 0
        # Вычисляем тематическую оценку изображения локально.
        def score(self, picture):
            self.calls += 1
            return 0.1 if self.calls == 1 else 0.9
    report = images.scan(project, cfg=cfg, http=object(), scorer=Scorer())
    assert report["state"] == "ready" and len(report["top"]) == 10
    assert report["downloaded"] == 12


# Проверяем сценарий: source block erases previous top and does not retry.
def test_source_block_erases_previous_top_and_does_not_retry(project, monkeypatch):
    cfg = image_config(project)
    monkeypatch.setenv("PIXABAY_API_KEY", "unused-in-mock")
    latest_path = project / images.RESULT_FILE
    latest_path.parent.mkdir(parents=True)
    latest_path.write_text(json.dumps({"state": "ready", "top": [{"rank": 1}]}))
    calls = []
    # Операция blocked: данного модуля.
    def blocked(*args):
        calls.append(1)
        raise AgentError("SOURCE_HTTP_429")
        yield
    monkeypatch.setattr(images, "candidates", blocked)
    report = images.scan(project, cfg=cfg, http=object(), scorer=object())
    assert report["state"] == "source_error" and report["top"] == []
    assert report["code"] == "SOURCE_HTTP_429" and calls == [1]
    assert images.latest(project)["top"] == []


# Проверяем сценарий: full scan visits all sources and reports empty results.
def test_full_scan_visits_all_sources_and_reports_empty_results(project, monkeypatch):
    cfg = image_config(project)
    # Последовательно обрабатываем элементы: cfg['sources'].
    for source in cfg["sources"]:
        source["enabled"] = True
    monkeypatch.setenv("PIXABAY_API_KEY", "unused-in-mock")
    visited = []
    # Операция empty_openverse: данного модуля.
    def empty_openverse(root, source):
        visited.append(source["id"])
        return []
    # Операция empty_pixabay: данного модуля.
    def empty_pixabay(http, root, source, key):
        visited.append(source["id"])
        return iter([])
    monkeypatch.setattr(openverse_source, "candidates", empty_openverse)
    monkeypatch.setattr(images, "candidates", empty_pixabay)
    report = images.scan(project, cfg=cfg, http=object(), scorer=object(), all_sources=True)
    assert len(visited) == len(set(visited)) == 30
    assert set(visited) == set(report["source_ids"])
    assert all(row["state"] == "no_results" for row in report["source_results"].values())
    assert report["state"] == "insufficient_candidates" and report["top"] == []


# Проверяем сценарий: full scan stops at rate limit and records unvisited sources.
def test_full_scan_stops_at_rate_limit_and_records_unvisited_sources(project, monkeypatch):
    cfg = image_config(project)
    # Последовательно обрабатываем элементы: cfg['sources'].
    for source in cfg["sources"]:
        source["enabled"] = True
    monkeypatch.setenv("PIXABAY_API_KEY", "unused-in-mock")
    called = []
    # Операция blocked: данного модуля.
    def blocked(root, source):
        called.append(source["id"])
        raise AgentError("SOURCE_HTTP_429")
    monkeypatch.setattr(openverse_source, "candidates", blocked)
    report = images.scan(project, cfg=cfg, http=object(), scorer=object(), all_sources=True)
    assert len(called) == 1 and report["state"] == "source_error"
    assert report["source_results"][called[0]]["code"] == "SOURCE_HTTP_429"
    assert sum(row["state"] == "not_scanned" for row in report["source_results"].values()) == 29
    assert report["top"] == []


# Проверяем сценарий: unused scan excludes source ids pages and binary duplicates.
def test_unused_scan_excludes_source_ids_pages_and_binary_duplicates(project, monkeypatch):
    cfg = image_config(project)
    cfg["top_n"] = 1
    monkeypatch.setenv("PIXABAY_API_KEY", "unused-in-mock")
    content = b"new-image"
    source = cfg["sources"][0]
    monkeypatch.setattr(images, "used_images", lambda root, channel_id="test": {
        "ids": {(source["id"], "1")}, "pages": {"https://pixabay.com/used"},
        "hashes": {hashlib.sha256(b"old-image").hexdigest()},
    })
    monkeypatch.setattr(images, "candidates", lambda *args: iter([
        {"id": str(i), "views": 20000, "likes": 200} for i in range(1, 5)]))
    monkeypatch.setattr(images, "verify_metadata", lambda http, root, source, key, item, cfg: ({
        "source_id": source["id"], "id": item["id"], "title": "Art", "author": "Artist",
        "page_url": "https://pixabay.com/used" if item["id"] == "2" else "https://pixabay.com/" + item["id"],
        "image_url": "https://pixabay.com/get/" + item["id"], "views": 20000, "likes": 200,
    }, None))
    monkeypatch.setattr(images, "image_bytes", lambda url, limit: b"old-image" if url.endswith("/3") else content)
    monkeypatch.setattr(images, "decode_image", lambda *args: Image.new("RGB", (512, 512)))
    # Структура Scorer объединяет связанные данные и операции модуля.
    class Scorer:
        # Вычисляем тематическую оценку изображения локально.
        def score(self, picture):
            return .9
    report = images.scan(project, cfg=cfg, http=object(), scorer=Scorer(), exclude_used=True)
    assert report["state"] == "ready" and report["top"][0]["id"] == "4"
    assert report["rejected"]["previously_used"] == 3


# Проверяем сценарий: publication preserves original image and keeps metrics in caption.
def test_publication_preserves_original_image_and_keeps_metrics_in_caption(project):
    path = project / "data/image-ranking/files/original.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), "navy").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = {"path": path.relative_to(project).as_posix(), "sha256": digest,
           "author": "Artist", "page_url": "https://example.org/art", "license": "CC0",
           "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
           "views": 20000, "likes": 200}
    result = images.publication_image(project, row)
    assert result == {"path": row["path"], "sha256": digest}
    assert Image.open(project / result["path"]).size == (800, 600)
    assert "20,000" in images.publication_body(row) and "Лайки: 200" in images.publication_body(row)


# Проверяем сценарий: owner image publication uses approved outbox and repauses.
def test_owner_image_publication_uses_approved_outbox_and_repauses(service, project, monkeypatch):
    cfg = image_config(project)
    service.activate(browser_config(project))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    service.connect("test", "-123")
    path = project / "data/image-ranking/files/image.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (512, 512), "navy").save(path)
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    row = {"rank": 1, "id": "123", "source_id": cfg["sources"][0]["id"],
           "sha256": sha, "path": path.relative_to(project).as_posix(),
           "title": "AI art", "author": "Автор", "page_url": "https://pixabay.com/illustrations/art-123/",
           "image_url": "https://pixabay.com/get/photo.png",
           "license": "Pixabay Content License", "license_url": "https://pixabay.com/service/license-summary/",
           "views": 20000, "likes": 200}
    report = {"state": "ready", "top": [row], "checked_at": datetime.now(timezone.utc).isoformat()}
    latest_path = project / images.RESULT_FILE
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    monkeypatch.setattr(images, "verify_metadata", lambda *a, **k: (
        {"page_url": row["page_url"], "license_url": row["license_url"],
         "author": row["author"], "image_url": row["image_url"]}, None))
    monkeypatch.setattr(images, "image_bytes", lambda *a, **k: content)
    captured = []
    # Операция sent: данного модуля.
    def sent(chat, body, attachment=None, *, request_id=None):
        captured.append((chat, body, attachment, request_id))
        return Delivery("confirmed", external_id="browser-test")
    monkeypatch.setattr(service.publisher, "publish", sent)
    result = images.publish_rank(service, 1, sha)
    assert result["state"] == "confirmed" and result["intent_id"]
    assert captured[0][0] == "-123" and captured[0][2]["type"] == "browser-image"
    assert captured[0][2]["payload"]["sha256"] == sha
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as db:
        assert db.get(Channel, "test").paused is True
        assert db.get(Draft, result["draft_id"]).state == "published"
        assert db.get(Intent, result["intent_id"]).state == "confirmed"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="IMAGE_ALREADY_STAGED"):
        images.publish_rank(service, 1, sha)


# Проверяем сценарий: openverse publication attributes author without invented metrics.
def test_openverse_publication_attributes_author_without_invented_metrics(service, project, monkeypatch):
    cfg = image_config(project)
    source = next(row for row in cfg["sources"] if row["type"] == "openverse_source")
    source["enabled"] = True
    (project / images.SOURCE_FILE).write_text(json.dumps(cfg), encoding="utf-8")
    service.activate(browser_config(project))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    service.connect("test", "-123")
    path = project / "data/image-ranking/files/openverse.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (512, 512), "purple").save(path)
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    identifier = "9ff3dd33-0705-46d9-97a6-0da64fe227dd"
    row = {"rank": 1, "id": identifier, "source_id": source["id"], "sha256": sha,
           "path": path.relative_to(project).as_posix(), "title": "Digital art",
           "author": "Artist", "page_url": "https://example.org/art/1",
           "image_url": openverse_source.API + identifier + "/thumb/?full_size=true",
           "license": "CC BY", "license_url": "https://creativecommons.org/licenses/by/4.0/",
           "views": 0, "likes": 0, "metrics_available": False, "local_relevance": 0.81}
    latest_path = project / images.RESULT_FILE
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps({"state": "ready", "top": [row],
                                       "checked_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    monkeypatch.setattr(openverse_source, "verify", lambda *a: (row, None))
    monkeypatch.setattr(openverse_source, "image_bytes", lambda *a: content)
    captured = []
    # Операция sent: данного модуля.
    def sent(chat, body, attachment=None, *, request_id=None):
        captured.append(body)
        return Delivery("confirmed", external_id="browser-test")
    monkeypatch.setattr(service.publisher, "publish", sent)
    result = images.publish_rank(service, 1, sha)
    assert result["state"] == "confirmed"
    assert "Автор: Artist" in captured[0] and row["license_url"] in captured[0]
    assert "Изображение опубликовано без" not in captured[0]
    assert "счётчиков просмотров и лайков нет" in captured[0]
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as db:
        assert db.get(Channel, "test").paused is True


# Проверяем сценарий: auto run selects once per four hour slot and dry run does not post.
def test_auto_run_selects_once_per_four_hour_slot_and_dry_run_does_not_post(service, project, monkeypatch):
    image_config(project)
    service.activate(browser_config(project))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    service.connect("test", "-123")
    row = {"rank": 1, "sha256": "a" * 64, "id": "123", "source_id": "pixabay_ai_illustrations",
           "page_url": "https://pixabay.com/illustrations/example-123/"}
    next_row = {**row, "rank": 2, "sha256": "b" * 64, "id": "456",
                "page_url": "https://pixabay.com/illustrations/example-456/"}
    scans, sends = [], []
    # Операция scanner: данного модуля.
    def scanner(root):
        scans.append(root)
        return {"state": "ready", "top": [row if len(scans) <= 2 else next_row]}
    # Операция publisher: данного модуля.
    def publisher(owner, rank, sha256):
        sends.append((rank, sha256))
        return {"state": "confirmed", "draft_id": "test-draft", "intent_id": "test-intent"}
    first = datetime(2026, 10, 10, 10, 0, tzinfo=timezone(timedelta(hours=3)))
    second = first.replace(hour=12)
    dry = images.auto_run(service, dry_run=True, at=first, scanner=scanner,
                          publisher=publisher)
    assert dry["state"] == "dry_run_ready" and sends == []
    assert not (project / images.AUTO_DIR / "2026-10-10-08.json").exists()
    live = images.auto_run(service, at=first, scanner=scanner, publisher=publisher)
    assert live["state"] == "confirmed" and sends == [(1, "a" * 64)]
    again = images.auto_run(service, at=first, scanner=scanner, publisher=publisher)
    assert again["state"] == "already_attempted" and len(scans) == 2 and len(sends) == 1
    following = images.auto_run(service, at=second, scanner=scanner, publisher=publisher)
    assert following["state"] == "confirmed" and following["slot"] == "2026-10-10-12"
    assert sends == [(1, "a" * 64), (2, "b" * 64)]


# Проверяем сценарий: default automation builds top from unused images.
def test_default_automation_builds_top_from_unused_images(service, project, monkeypatch):
    image_config(project)
    service.activate(browser_config(project))
    monkeypatch.setattr(service.publisher, "inspect", lambda chat: {
        "publish": True, "binding_hash": fingerprint(service.settings.app["max"]),
    })
    service.connect("test", "-123")
    calls = []
    # Операция scanner: данного модуля.
    def scanner(root, **options):
        calls.append(options)
        return {"state": "ready", "top": [{"rank": 1, "sha256": "a" * 64,
                "id": "new", "source_id": "pixabay_ai_illustrations",
                "page_url": "https://pixabay.com/illustrations/new/"}]}
    monkeypatch.setattr(images, "scan", scanner)
    result = images.auto_run(service, dry_run=True)
    assert result["state"] == "dry_run_ready"
    assert calls == [{"exclude_used": True}]


# Проверяем сценарий: openverse source validates rights and uses fixed media proxy.
def test_openverse_source_validates_rights_and_uses_fixed_media_proxy(project, monkeypatch):
    config = image_config(project)
    assert len(config["sources"]) == 30
    source = next(row for row in config["sources"] if row["type"] == "openverse_source")
    identifier = "9ff3dd33-0705-46d9-97a6-0da64fe227dd"
    detail = {"id": identifier, "source": source["source_name"], "license": "by",
              "license_url": "https://creativecommons.org/licenses/by/4.0/",
              "foreign_landing_url": source["site_url"].rstrip("/") + "/art/1",
              "creator": "Artist", "title": "Art"}
    called = []
    # Операция fetch: данного модуля.
    def fetch(url, *, image=False):
        called.append((url, image))
        return b"test-image" if image else json.dumps(
            detail if url.endswith(identifier + "/") else {"results": [{"id": identifier}]}
        ).encode()
    monkeypatch.setattr(openverse_source, "_fetch", fetch)
    rows = openverse_source.candidates(project, source)
    record, reason = openverse_source.verify(project, source, rows[0])
    assert reason is None and record["metrics_available"] is False
    assert record["license"] == "CC BY" and record["page_url"] == detail["foreign_landing_url"]
    assert openverse_source.image_bytes(identifier) == b"test-image"
    assert all(url.startswith(openverse_source.API) for url, _ in called)
    assert called[-1][0].endswith("/thumb/?full_size=true")
    assert openverse_source.verify(project, source, {"id": "not-a-uuid"})[1] == "openverse_id_invalid"
