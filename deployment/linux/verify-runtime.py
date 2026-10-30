"""Read-only infrastructure checks; this script never approves or publishes posts."""
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import text

from legacy_guard import refuse_legacy
from max_postiz_agent.cli import load_env
from max_postiz_agent.config import Settings
from max_postiz_agent.db import Database

root = Path('/opt/max-postiz')
refuse_legacy(root)
load_env(root)
db = Database(Settings.load(root))
with db.engine.connect() as conn:
    state = {name: conn.scalar(text(f'SELECT count(*) FROM {name}')) for name in  # noqa: S608 -- fixed table allowlist
             ['stories', 'drafts', 'materials', 'hub_targets', 'hub_variants', 'hub_dispatches']}
    state['active_dispatches'] = conn.scalar(text("SELECT count(*) FROM hub_dispatches WHERE state NOT IN ('published','owner_attested','cancelled','failed')"))
    state['unpaused_targets'] = conn.scalar(text('SELECT count(*) FROM hub_targets WHERE NOT paused'))
    state['mutations'] = conn.scalar(text('SELECT count(*) FROM hub_bridge_posts WHERE mutation_owner IS NOT NULL'))
db.engine.dispose()
report = {'database': state}
base = 'http://10.20.30.45:8090'
with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
    report['hub_http'] = client.get(base + '/hub').status_code
    report['owner_display_anonymous'] = client.get(base + '/api/owner-browser/vnc.html').status_code
    report['owner_display_viewer'] = client.get(base + '/api/owner-browser/vnc.html',
        headers={'Authorization': 'Bearer ' + os.environ['APP_VIEWER_SECRET']}).status_code
    login = client.post(base + '/api/v1/session/login',
                        headers={'Authorization': 'Bearer ' + os.environ['APP_AUTH_SECRET']})
    report['owner_login_http'] = login.status_code
    display = client.get(base + '/api/owner-browser/vnc.html')
    report['owner_display_authenticated'] = display.status_code
    report['owner_display_html'] = 'noVNC' in display.text
    report['owner_display_foreign_origin'] = client.get(base + '/api/owner-browser/vnc.html',
        headers={'Origin': 'https://example.org'}).status_code
    try:
        spelling = client.post('http://127.0.0.1:8010/v2/check',
                               data={'language': 'ru-RU', 'text': 'Это проверка русского текста.'})
        report['spelling_http'] = spelling.status_code
        if spelling.status_code == 200:
            report['spelling_matches'] = len(spelling.json()['matches'])
    except httpx.HTTPError:
        report['spelling_http'] = None
(root / 'data/linux-runtime-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
