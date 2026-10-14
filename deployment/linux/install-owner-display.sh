#!/bin/bash
set -euo pipefail
cd /opt/max-postiz
install -m 0644 deployment/linux/max-postiz-{display,vnc,novnc,login}.service /etc/systemd/system/
install -m 0644 deployment/linux/owner-browser.nginx.conf /etc/nginx/snippets/editorial-owner-browser.conf
cat >/etc/nginx/conf.d/editorial-owner-origin.conf <<'NGINX'
map $http_origin $owner_browser_origin_allowed {
  default 0;
  "" 1;
  "http://10.20.30.45:8090" 1;
}
NGINX
python3 - <<'PY'
from pathlib import Path
path = Path('/etc/nginx/sites-available/editorial')
text = path.read_text()
include = '  include /etc/nginx/snippets/editorial-owner-browser.conf;'
if include not in text:
    anchor = '  listen 10.20.30.45:8090;'
    assert text.count(anchor) == 1
    path.write_text(text.replace(anchor, anchor + '\n' + include))
PY
systemctl daemon-reload
nginx -t
systemctl reload nginx
systemctl start max-postiz-display max-postiz-vnc max-postiz-novnc
echo 'Private display ready; sign in as Hub owner before opening /api/owner-browser/vnc.html'
