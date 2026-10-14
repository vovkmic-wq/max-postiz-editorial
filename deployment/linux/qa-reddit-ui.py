"""Read-only owner UI check; no material submissions, mailbox changes or publications."""
import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from max_postiz_agent.cli import load_env

root = Path('/opt/max-postiz')
load_env(root)
base = 'http://10.20.30.45:8090'
errors = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel='chrome', headless=True, chromium_sandbox=True)
    context = browser.new_context(viewport={'width':1440, 'height':1000})
    for _ in range(10):
        response = context.request.get(base + '/api/hub/reddit')
        if response.status != 502:
            break
        time.sleep(0.5)
    assert response.status == 401
    response = context.request.post(base + '/api/v1/session/login',
        headers={'Authorization':'Bearer ' + os.environ['APP_AUTH_SECRET']})
    assert response.status == 200
    page = context.new_page()
    page.on('pageerror', lambda _: errors.append('pageerror'))
    page.goto(base + '/hub', wait_until='networkidle')
    page.get_by_role('heading', name='Reddit: IFTTT и ручной отбор', exact=True).wait_for()
    assert page.get_by_label('Английский текст', exact=True).is_visible()
    assert page.get_by_label('Канал MAX', exact=True).input_value() == 'max_test'
    page.get_by_text('Подключить IFTTT через Gmail', exact=True).click()
    assert page.get_by_label('Gmail', exact=True).input_value()
    assert page.get_by_label('Пароль приложения', exact=True).input_value() == ''
    assert not page.get_by_label('Забирать письма IFTTT', exact=True).is_checked()
    output = root / 'data/linux-qa'
    output.mkdir(exist_ok=True)
    page.locator('#reddit-intake').screenshot(path=str(output/'reddit-intake.png'))
    assert not errors
    page.reload(wait_until='networkidle')
    assert page.get_by_label('Английский текст', exact=True).is_visible()
    browser.close()
report = {'owner_ui':True, 'reload_verified':True, 'page_errors':len(errors),
          'mail_connected':False, 'external_posts':0, 'new_materials':0}
(root/'data/reddit-intake-ui-check.json').write_text(json.dumps(report))
print(json.dumps(report))
