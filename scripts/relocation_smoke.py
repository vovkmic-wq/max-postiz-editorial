"""Verify a source archive at a new path without inherited runtime credentials."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists():
        raise RuntimeError("Use a new empty relocation path")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(args.archive) as archive:
        manifest = json.loads(archive.read("package-manifest.json"))
        for name, digest in manifest["hashes"].items():
            path = (destination / name).resolve()
            if not path.is_relative_to(destination):
                raise RuntimeError("Archive path invalid")
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise RuntimeError("Archive hash mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    env = {key: value for key, value in os.environ.items() if not any(word in key.upper()
           for word in ("AUTH", "TOKEN", "SECRET", "PASSWORD", "DATABASE", "OPENCODE", "POSTIZ", "MAX_EXISTING", "BACKUP"))}
    env["PYTHONPATH"], env["PYTHONUTF8"] = str(destination / "src"), "1"
    def run(*arguments):
        result = subprocess.run([sys.executable, *arguments], cwd=destination, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise RuntimeError("Relocation command failed")
        return result.stdout
    run("-m", "max_postiz_agent.hub_cli", "init", "--sqlite")
    doctor = json.loads(run("-m", "max_postiz_agent.hub_cli", "doctor"))
    assert doctor["database"] == "sqlite"
    assert not doctor["postiz_configured"]
    assert all(not target["enabled"] for target in doctor["targets"])
    run("-m", "max_postiz_agent.hub_cli", "tick")
    print(json.dumps({"state": "passed", "files_verified": len(manifest["hashes"]),
                      "database": "explicit_development_sqlite", "targets_disabled": True,
                      "dependencies": "current_venv", "path": str(destination)}))


if __name__ == "__main__":
    main()
