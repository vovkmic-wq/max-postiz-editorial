"""Check the installed local Postiz editor in an isolated browser session."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    access = json.loads((root / "data/postiz-access.json").read_text(encoding="utf-8"))
    output = root / "docs/images"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        login = context.request.post(access["base_url"] + "/api/auth/login", data={
            "email": access["email"], "password": access["password"], "provider": "LOCAL"})
        if login.status != 200:
            raise RuntimeError("POSTIZ_UI_LOGIN_FAILED")
        page = context.new_page()
        page.on("pageerror", lambda _: errors.append("pageerror"))
        response = page.goto(access["base_url"], wait_until="domcontentloaded", timeout=60000)
        page.get_by_text("Calendar", exact=True).first.wait_for(state="visible", timeout=30000)
        if not response or response.status >= 400 or "/auth/" in page.url:
            raise RuntimeError("POSTIZ_UI_SESSION_FAILED")
        # User/self is a live check of the same browser cookie used by the editor.
        owner = context.request.get(access["base_url"] + "/api/user/self")
        if owner.status != 200:
            raise RuntimeError("POSTIZ_UI_OWNER_FAILED")
        page.screenshot(path=str(output / "postiz-desktop.png"), full_page=True)
        page.reload(wait_until="domcontentloaded")
        page.get_by_text("Calendar", exact=True).first.wait_for(state="visible", timeout=30000)
        if "/auth/" in page.url:
            raise RuntimeError("POSTIZ_UI_RELOAD_FAILED")
        print(json.dumps({"state": "passed", "http_status": response.status,
                          "authenticated": True, "reload_verified": True,
                          "page_errors": len(errors), "url": page.url, "title": page.title()}))
        browser.close()
        if errors:
            raise RuntimeError("POSTIZ_UI_SCRIPT_ERRORS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Playwright exceptions can contain request cookies and headers.
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("POSTIZ_UI_") else type(exc).__name__
        print(json.dumps({"state": "failed", "code": code}))
        raise SystemExit(1) from None
