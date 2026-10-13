"""Audit runtime state, source provenance and source archive without exposing secrets."""
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path

from sqlalchemy import func, select

from max_postiz_agent.cli import load_env
from max_postiz_agent import __version__
from max_postiz_agent.db import Draft, Intent, Story
from max_postiz_agent.hub_cli import build
from max_postiz_agent.hub_models import Dispatch, Target


def main():
    root = Path(__file__).resolve().parents[1]
    load_env(root)
    baseline = json.loads((root / "docs/reuse-manifest.json").read_text(encoding="utf-8"))
    unchanged = sum(hashlib.sha256((Path("C:/script/20261005-Max") / item["source"]).read_bytes()).hexdigest() == item["source_sha256"] for item in baseline)
    service = build(root)
    try:
        with service.db.session() as session:
            counts = {"stories": session.scalar(select(func.count()).select_from(Story)),
                      "archived_drafts": session.scalar(select(func.count()).select_from(Draft).where(Draft.state == "archived")),
                      "archived_legacy_intents": session.scalar(select(func.count()).select_from(Intent).where(Intent.state == "archived")),
                      "new_dispatches": session.scalar(select(func.count()).select_from(Dispatch))}
            paused = all(t.paused for t in session.scalars(select(Target)))
    finally:
        service.close()
    values = []
    for path in (root / ".env.local", root / "deployment/postiz/.env", root / "data/postiz-access.json", root / "data/opencode-runtime/access.json", root / "data/postgres-access.json"):
        if not path.exists():
            continue
        if path.suffix == ".json":
            values.extend(str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k in {"password", "secret", "token", "api_key"})
        else:
            values.extend(line.split("=", 1)[1].strip() for line in path.read_text(encoding="utf-8").splitlines()
                          if "=" in line and any(word in line.split("=", 1)[0] for word in ("SECRET", "KEY", "TOKEN", "PASSWORD")))
    secrets = [value.encode() for value in values if len(value) >= 12]
    def check(name, raw):
        if name.startswith(("data/", ".venv/", "node_modules/")) or (Path(name).name.startswith(".env") and Path(name).name != ".env.example"):
            raise RuntimeError("Private runtime file in release package")
        if any(value in raw for value in secrets):
            raise RuntimeError("Runtime secret found in release package")

    scanned = ["editorial-hub.zip", f"max_postiz_agent-{__version__}-py3-none-any.whl",
               f"max_postiz_agent-{__version__}.tar.gz"]
    for name in scanned[:2]:
        with zipfile.ZipFile(root / "dist" / name) as archive:
            for entry in archive.namelist():
                check(entry, archive.read(entry))
    with tarfile.open(root / "dist" / scanned[2], "r:gz") as archive:
        for entry in archive:
            if entry.isfile():
                stream = archive.extractfile(entry)
                if stream is None:
                    raise RuntimeError("Release archive member missing")
                with stream:
                    check(entry.name.split("/", 1)[-1], stream.read())
    report = {"source_files": len(baseline), "source_files_unchanged": unchanged,
              "runtime_counts": counts, "all_targets_paused": paused, "source_archive_secret_scan": "passed",
              "release_packages_secret_scan": "passed", "scanned_packages": scanned,
              "artifacts": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (root / "dist").iterdir() if path.suffix in {".zip", ".whl", ".gz"}}}
    (root / "dist/release-manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
