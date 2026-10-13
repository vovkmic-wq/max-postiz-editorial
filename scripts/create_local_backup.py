"""Create an encrypted project-local backup; keep its key in ignored local settings."""
from pathlib import Path
from cryptography.fernet import Fernet
from max_postiz_agent.hub_cli import main

root = Path(__file__).resolve().parents[1]
env = root / ".env.local"
text = env.read_text(encoding="utf-8")
if not any(line.startswith("HUB_BACKUP_KEY=") and line.split("=", 1)[1].strip() for line in text.splitlines()):
    text = "\n".join(line for line in text.splitlines() if not line.startswith("HUB_BACKUP_KEY="))
    env.write_text(text + "\nHUB_BACKUP_KEY=" + Fernet.generate_key().decode() + "\n", encoding="utf-8")
main(["--root", str(root), "backup", str(root / "data/backups/editorial-hub.enc")])
