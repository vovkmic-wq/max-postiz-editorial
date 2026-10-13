"""Browser QA on the real paused panel and an isolated editorial fixture."""
import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from max_postiz_agent.cli import load_env
from max_postiz_agent.config import Settings, read_topic
from max_postiz_agent.db import Database
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_api import hub_app


def main():
    root = Path(__file__).resolve().parents[1]
    load_env(root)
    key = os.environ["APP_AUTH_SECRET"]
    output = root / "docs/images"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://127.0.0.1:8090/hub")
        page.locator("#key").fill(key)
        page.locator("#signin").click()
        page.locator("#main").wait_for(state="visible")
        assert "postgresql" in page.locator("#runtime").inner_text()
        assert "max_test" in page.locator("#targets").inner_text()
        assert page.locator("#key").input_value() == ""
        page.screenshot(path=str(output / "panel-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "panel-mobile.png"), full_page=True)
        browser.close()
    # The second server has fake transports, SQLite and temporary data only.
    sys.path.insert(0, str(root / "tests"))
    from test_workflow import seed
    from conftest import write_topic
    os.environ.pop("DATABASE_URL", None)
    with tempfile.TemporaryDirectory(prefix="hub-ui-") as directory, ExitStack() as cleanup:
        project = Path(directory)
        shutil.copytree(root / "schemas", project / "schemas")
        shutil.copytree(root / "prompts", project / "prompts")
        (project / "config").mkdir()
        shutil.copy2(root / "config/app.example.json", project / "config/app.json")
        topic = read_topic(root)
        topic["channels"][0]["enabled"] = True
        topic["channels"][0]["publication"]["mode"] = "review"
        write_topic(project, topic)
        sources = json.loads((root / "config/sources.example.json").read_text(encoding="utf-8-sig"))
        sources["sources"][0]["enabled"] = True
        sources["sources"][0]["rights"].update(verified=True, allow_storage=True, allow_derivative=True, evidence_ref="UI_QA_FIXTURE")
        (project / "config/sources.json").write_text(json.dumps(sources), encoding="utf-8")
        targets = [{"id": p, "profile_id": "main", "platform": p, "route": "manual" if p == "pikabu" else "max_api",
                    "enabled": True, "destination": "fixture", "options": {"max_chars": 15000}} for p in ("max", "pikabu")]
        (project / "config/publishing.json").write_text(json.dumps({"schema_version": "1.0", "postiz": {}, "tasks": [], "targets": targets}), encoding="utf-8")
        settings = Settings.load(project)
        db = Database(settings)
        db.initialize()
        service = HubService(settings, db)
        cleanup.callback(service.close)
        service.activate()
        draft = service.edit_story("main", seed(service))
        service.make_variants(draft)
        server = uvicorn.Server(uvicorn.Config(hub_app(service), host="127.0.0.1", port=8091, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto("http://127.0.0.1:8091/hub", wait_until="networkidle")
                page.locator("#key").fill(key)
                page.locator("#signin").click()
                page.locator("#variants article").nth(1).wait_for()
                card = page.locator("#variants article").filter(has=page.get_by_role("textbox", name="Теги через запятую"))
                card.get_by_role("textbox", name="Заголовок", exact=True).fill("Проверка редактора Пикабу")
                card.get_by_role("textbox", name="Теги через запятую").fill("Видео, Контент нейросетей")
                card.get_by_role("button", name="Сохранить новую версию").click()
                page.get_by_text("Проверка редактора Пикабу", exact=False).first.wait_for()
                assert "Версия 2" in page.locator("#variants").inner_text()
                page.screenshot(path=str(output / "editor-fixture.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(output / "editor-mobile-fixture.png"), full_page=True)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(10)
            service.close()
    if errors:
        raise RuntimeError("Browser script errors")
    print(json.dumps({"real_panel": "passed", "fixture_editor_save": "passed", "mobile_layout": "passed", "browser_errors": len(errors)}))


if __name__ == "__main__":
    main()
