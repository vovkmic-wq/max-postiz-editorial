"""Restore the real encrypted backup into an isolated schema of the test database."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import create_engine, text
from max_postiz_agent.backup import restore
from max_postiz_agent.cli import load_env
from max_postiz_agent.config import Settings
from max_postiz_agent.db import Database, Draft, Story, uid
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import Target


def main():
    root = Path(__file__).resolve().parents[1]
    load_env(root)
    url = os.environ["TEST_DATABASE_URL"]
    schema = "hub_backup_" + uid().replace("-", "")
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))  # noqa: S608 -- internal UUID only
    try:
        with tempfile.TemporaryDirectory(prefix="hub-backup-verify-") as directory:
            project = Path(directory)
            shutil.copytree(root / "schemas", project / "schemas")
            shutil.copytree(root / "prompts", project / "prompts")
            (project / "config").mkdir()
            for name in ("app", "sources", "publishing"):
                shutil.copy2(root / ("config/" + name + ".json"), project / ("config/" + name + ".json"))
            shutil.copy2(root / "TOPIC.md", project / "TOPIC.md")
            isolated = url + ("&" if "?" in url else "?") + urlencode({"options": "-csearch_path=" + schema})
            settings = Settings.load(project)
            db = Database(settings, url=isolated)
            db.initialize()
            service = HubService(settings, db, bootstrap_targets=False)
            try:
                restore(service, root / "data/backups/editorial-hub.enc", os.environ["HUB_BACKUP_KEY"].encode())
                with db.session() as session:
                    result = {"state": "passed", "stories": session.query(Story).count(),
                              "archived_drafts": session.query(Draft).filter(Draft.state == "archived").count(),
                              "all_targets_paused": all(t.paused for t in session.query(Target)), "database": "postgresql"}
                assert result["stories"] == 19 and result["archived_drafts"] == 19 and result["all_targets_paused"]
            finally:
                service.close()
    finally:
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))  # noqa: S608 -- internal UUID only
        admin.dispose()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
