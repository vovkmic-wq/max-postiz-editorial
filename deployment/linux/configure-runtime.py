"""Rebase private runtime paths after an owner-authorized Linux migration."""
import json
import secrets
from pathlib import Path
from urllib.parse import quote

root = Path('/opt/max-postiz')
origin = 'http://10.20.30.45:4007'
marker = root / 'data/linux-runtime-configured'
if marker.exists():
    raise SystemExit('Private Linux settings already configured; refusing to rotate database credentials')


def env_file(path):
    return dict(line.split('=', 1) for line in path.read_text(encoding='utf-8-sig').splitlines()
                if '=' in line and not line.startswith('#'))


def save_env(path, value):
    path.write_text(''.join(f'{k}={v}\n' for k, v in value.items()), encoding='utf-8')
    path.chmod(0o600)


env = env_file(root / '.env.local')
deployment = env_file(root / 'deployment/postiz/.env')
password = secrets.token_urlsafe(32)
deployment.update(HUB_DB_PASSWORD=password, POSTIZ_PUBLIC_ORIGIN=origin)
env.update(DATABASE_URL=f'postgresql+psycopg://hub_admin:{quote(password)}@127.0.0.1:55438/editorial_hub',
           POSTIZ_BASE_URL='http://127.0.0.1:14007/api/public/v1',
           POSTIZ_PUBLIC_ORIGIN=origin,
           PUBLIC_BASE_URL='http://10.20.30.45:8090', OPENCODE_BASE_URL='http://127.0.0.1:4108')
for key in ('TEST_DATABASE_URL', 'MAX_EXISTING_BROWSER_PROFILE', 'OPENCODE_AUTH_SECRET'):
    env.pop(key, None)
save_env(root / '.env.local', env)
save_env(root / 'deployment/postiz/.env', deployment)
app = json.loads((root / 'config/app.json').read_text(encoding='utf-8-sig'))
app['max']['browser'].update(profile_dir='data/max-browser', channel='chrome', headless=True)
app['api'].update(bind_host='0.0.0.0', port=8091)  # noqa: S104 -- firewall permits Docker bridge only
(root / 'config/app.json').write_text(json.dumps(app, ensure_ascii=False, indent=2), encoding='utf-8')
access = root / 'data/postiz-access.json'
if access.exists():
    value = json.loads(access.read_text(encoding='utf-8-sig'))
    value['base_url'] = origin
    access.write_text(json.dumps(value), encoding='utf-8')
    access.chmod(0o600)
print('Private Linux settings prepared; publication remains paused')
marker.write_text('0.5.0\n', encoding='utf-8')
