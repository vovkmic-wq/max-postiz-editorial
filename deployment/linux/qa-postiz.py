"""Authenticate and inspect the LAN frontend without scheduling any publication."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path('/opt/max-postiz')
access = json.loads((root / 'data/postiz-access.json').read_text())
output = root / 'data/linux-qa'
output.mkdir(exist_ok=True)
errors, requests = [], []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel='chrome', headless=True, chromium_sandbox=True)
    context = browser.new_context(viewport={'width':1440, 'height':960})
    response = context.request.post(access['base_url'] + '/api/auth/login', data={
        'email':access['email'], 'password':access['password'], 'provider':'LOCAL'})
    assert response.status == 200, 'POSTIZ_LOCAL_LOGIN_FAILED'
    page = context.new_page()
    page.on('pageerror', lambda _: errors.append('pageerror'))
    page.on('request', lambda request: requests.append(request.url))
    response = page.goto(access['base_url'], wait_until='domcontentloaded', timeout=90000)
    page.get_by_text('Calendar', exact=True).first.wait_for(timeout=60000)
    assert response.status == 200 and '/auth/' not in page.url, 'POSTIZ_UI_SESSION_FAILED'
    assert context.request.get(access['base_url'] + '/api/user/self').status == 200
    page.screenshot(path=str(output/'postiz-lan.png'), full_page=True)
    page.reload(wait_until='domcontentloaded')
    page.get_by_text('Calendar', exact=True).first.wait_for(timeout=60000)
    assert '/auth/' not in page.url
    legacy = [u for u in requests if u.startswith(('http://localhost:4007','http://127.0.0.1:4007'))]
    assert not errors and not legacy, 'POSTIZ_UI_RUNTIME_ERRORS'
    report = {'authenticated':True, 'reload_verified':True, 'page_errors':len(errors),
              'legacy_origin_requests':len(legacy), 'external_posts':0, 'url':page.url}
    browser.close()
(root/'data/linux-postiz-ui-check.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
