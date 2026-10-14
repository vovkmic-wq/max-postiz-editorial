#!/bin/bash
# Ubuntu's existing AppArmor policy supports the root-owned Chrome installation.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
  -o /var/cache/editorial/google-chrome.deb
apt-get install -y /var/cache/editorial/google-chrome.deb
google-chrome --version
test -f /etc/apparmor.d/chrome
python3 - <<'PY'
import json
from pathlib import Path
path = Path('/opt/max-postiz/config/app.json')
value = json.loads(path.read_text())
value['max']['browser']['channel'] = 'chrome'
path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
PY
# Chromium sandboxing remains enabled; no global AppArmor/sysctl relaxation.
systemctl restart max-postiz-hub
