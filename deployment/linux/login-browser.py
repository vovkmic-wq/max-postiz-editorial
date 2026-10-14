"""Open the isolated Linux MAX profile for manual owner authentication only."""
import json
from pathlib import Path

from max_postiz_agent.browser_runtime import login
from max_postiz_agent.cli import load_env
from max_postiz_agent.config import Settings

root = Path('/opt/max-postiz')
load_env(root)
settings = Settings.load(root)
destination = '-79860712657053'
login(settings, destination, timeout=4*3600)
report = {'state': 'channel_access_confirmed', 'destination': destination,
          'live_delivery_verified': False, 'new_posts': 0}
(root / 'data/max-linux-login.json').write_text(json.dumps(report), encoding='utf-8')
print('Linux owner profile is authorized for the expected channel; no publication performed.', flush=True)
