"""Owner-authorized read-only Reddit OAuth; tokens stay in ignored local storage."""
import argparse
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('action', choices=['status', 'authorize'])
    args = parser.parse_args()
    root = args.root.resolve()
    load_env(root)
    path = root / 'config/reddit_automation.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    options = config.get('reddit', {})
    approved = options.get('access_approved') is True
    values = {name: os.getenv('REDDIT_' + name.upper(), '')
              for name in ['client_id', 'client_secret', 'refresh_token', 'user_agent']}
    if args.action == 'status':
        print(json.dumps({'access_approved': approved,
            'translation_approved': options.get('translation_and_redistribution_approved') is True,
            'configured': {k: bool(v) for k, v in values.items()},
            'automation_enabled': config.get('enabled') is True,
            'live_reddit_verified': False}))
        return
    if not approved:
        raise AgentError('REDDIT_APPROVAL_REQUIRED', 409)
    if not values['client_id'] or not values['client_secret'] or not values['user_agent']:
        raise AgentError('REDDIT_CLIENT_SETTINGS_REQUIRED', 409)
    state = secrets.token_urlsafe(32)
    redirect = 'http://localhost:8767/callback'
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            supplied = query.get('state', [''])[0]
            code = query.get('code', [''])[0]
            valid = (parsed.path == '/callback' and secrets.compare_digest(supplied, state)
                     and 1 <= len(code) <= 1024 and not query.get('error'))
            if valid:
                result['code'] = code
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'OAuth callback received. Return to the project.' if valid else b'OAuth callback rejected.')

    url = 'https://www.reddit.com/api/v1/authorize?' + urlencode({
        'client_id': values['client_id'], 'response_type': 'code', 'state': state,
        'redirect_uri': redirect, 'duration': 'permanent', 'scope': 'read'})
    private = root / 'data/reddit-oauth-link.txt'
    private.parent.mkdir(exist_ok=True)
    private.write_text(url, encoding='utf-8')
    private.chmod(0o600)
    print('Open data/reddit-oauth-link.txt in the owner browser. Forward SSH port 8767 for a Linux VM.', flush=True)
    deadline = time.monotonic() + 600
    with HTTPServer(('127.0.0.1', 8767), Callback) as server:
        server.timeout = 1
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise AgentError('REDDIT_OAUTH_TIMEOUT', 408)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        response = client.post('https://www.reddit.com/api/v1/access_token',
            auth=(values['client_id'], values['client_secret']),
            headers={'User-Agent': values['user_agent']},
            data={'grant_type': 'authorization_code', 'code': result['code'], 'redirect_uri': redirect})
    if response.status_code != 200:
        raise AgentError('REDDIT_OAUTH_REJECTED', 403)
    token = response.json().get('refresh_token')
    if not isinstance(token, str) or not token or any(x in token for x in '\r\n'):
        raise AgentError('REDDIT_OAUTH_REFRESH_TOKEN_REQUIRED', 409)
    env = root / '.env.local'
    lines = env.read_text(encoding='utf-8-sig').splitlines()
    lines = [line for line in lines if not line.startswith('REDDIT_REFRESH_TOKEN=')]
    env.write_text('\n'.join([*lines, 'REDDIT_REFRESH_TOKEN=' + token]) + '\n', encoding='utf-8')
    env.chmod(0o600)
    private.unlink(missing_ok=True)
    print('Refresh token saved privately. Collection and publication remain gated by the approved configuration.')


if __name__ == '__main__':
    try:
        main()
    except (AgentError, httpx.HTTPError, ValueError, KeyError) as exc:
        print(json.dumps({'error': exc.code if isinstance(exc, AgentError) else 'REDDIT_OAUTH_FAILED'}))
        raise SystemExit(1) from None
