"""Не позволяем прежним setup-командам вернуть Hub после серверной миграции."""
import subprocess
from pathlib import Path

MESSAGE = "LEGACY_COMMAND_RETIRED: после миграции используйте docs/VM_EDITORIAL.md"


def refuse_legacy(root=Path("/opt/max-postiz"), *, runner=None, unit_paths=None):
    marker = root / "data/hourly-articles/postgresql-authoritative.local.json"
    paths = unit_paths if unit_paths is not None else (
        Path("/etc/systemd/system/max-postiz-editorial.service"),
        Path("/usr/lib/systemd/system/max-postiz-editorial.service"),
        Path("/lib/systemd/system/max-postiz-editorial.service"),
    )
    if marker.exists() or any(path.exists() for path in paths):
        raise SystemExit(MESSAGE)
    run = runner or subprocess.run
    try:
        value = run(["systemctl", "show", "--property=LoadState", "--value",
                     "max-postiz-editorial.service"], capture_output=True, text=True,
                    timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit("LEGACY_STATE_UNAVAILABLE: проверьте docs/VM_EDITORIAL.md") from None
    if value.stdout.strip() != "not-found" or value.returncode != 0:
        # Неизвестное состояние также не даёт права перезаписывать сеть или ключи.
        raise SystemExit(MESSAGE)


if __name__ == "__main__":
    refuse_legacy()
