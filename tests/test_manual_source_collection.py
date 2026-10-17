import json

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.db import Cursor, Database, Job
from max_postiz_agent.service import Service


@pytest.mark.parametrize("mode", [None, "scheduled", "manual"])
def test_manual_collection_skips_background_jobs_and_preserves_scheduled_default(project, mode):
    path = project / "config/sources.json"
    sources = json.loads(path.read_text(encoding="utf-8"))
    source = sources["sources"][0]
    if mode is not None:
        source["collection_mode"] = mode
    path.write_text(json.dumps(sources), encoding="utf-8")
    settings = Settings.load(project)
    db = Database(settings)
    db.initialize()
    service = Service(settings, db)
    try:
        service.activate()
        with db.session() as session:
            original_due = session.get(Cursor, source["id"]).next_due
        service.schedule_sources()
        service.schedule_sources()
        with db.session() as session:
            jobs = list(session.scalars(select(Job).where(Job.kind == "collect")))
            assert len(jobs) == (0 if mode == "manual" else 1)
            if jobs:
                assert jobs[0].subject_id == source["id"]
                assert jobs[0].state == "queued"
            assert session.get(Cursor, source["id"]).next_due == original_due
        assert settings.source(source["id"])["enabled"] is True
    finally:
        service.close()


def test_unknown_collection_mode_is_rejected_by_source_schema(project):
    path = project / "config/sources.json"
    sources = json.loads(path.read_text(encoding="utf-8"))
    sources["sources"][0]["collection_mode"] = "automatic-ish"
    path.write_text(json.dumps(sources), encoding="utf-8")
    with pytest.raises(AgentError):
        Settings.load(project)
