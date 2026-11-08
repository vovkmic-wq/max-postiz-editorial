"""Real PostgreSQL CAS/atomicity; Postiz/media stay in-process stubs."""
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.hub_models import RemoteDraft, Target, Variant, VariantRevision
from test_max_plaintext_contract import core_stage, sync_capture
from pg_test_contract import CONTRACT_ENV, endpoint_spec, verify_identity

pytest_plugins = ["test_hub_postgres"]


@pytest.fixture
def guarded_pg_hub(request):
    # Validate before invoking the legacy fixture, which immediately creates a schema.
    url = os.getenv("TEST_DATABASE_URL")
    explicit = os.getenv(CONTRACT_ENV)
    contract = endpoint_spec("routes", url, Path(__file__).resolve().parents[1]) if explicit else None
    if contract is None:
        if not url:
            pytest.skip("TEST_DATABASE_URL is not configured")
        parsed = None
        try:
            parsed = make_url(url)
        except (ArgumentError, ValueError):
            pass
        if (parsed is None or parsed.drivername != "postgresql+psycopg" or parsed.query
                or not re.fullmatch(r"(?:test_[a-z0-9_]+|[a-z0-9_]+_tests)", parsed.database or "")):
            pytest.fail("Dedicated PostgreSQL test database required (value withheld)", pytrace=False)
    admin = create_engine(url)
    try:
        assert admin.dialect.name == "postgresql"
        if contract is not None:
            with admin.connect() as connection:
                verify_identity(connection, contract)
    finally:
        admin.dispose()
    value = request.getfixturevalue("pg_hub")
    assert value[0].db.engine.dialect.name == "postgresql"
    return value


def test_postgres_plaintext_and_synced_revision_are_same_snapshot(guarded_pg_hub, monkeypatch):
    core, before, after, _, _ = core_stage(guarded_pg_hub[0], monkeypatch)
    assert before["body"] == after["body"]
    assert core.sync_remote(after["variant_id"])["state"] == "unchanged"
    with core.db.session() as session:
        draft = session.get(RemoteDraft, after["variant_id"])
        assert draft.synced_revision == after["revision"] == 2
        assert draft.remote_hash == fingerprint({"body": before["body"]})


def test_postgres_two_fresh_reads_cannot_import_same_edit_twice(guarded_pg_hub, monkeypatch):
    core, _, after, _, records = core_stage(guarded_pg_hub[0], monkeypatch)
    ident = after["variant_id"]
    expected, target = sync_capture(core, ident)
    remote = copy.deepcopy(records["fixture-post"])
    remote["content"] += " Changed &amp;quot;literal&amp;quot;."
    def import_once():
        try:
            return core.bridge.import_post(remote, target, expected_local=expected)["state"]
        except AgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: import_once(), range(2)))
    assert sorted(outcomes) == ["REMOTE_EDIT_CONFLICT", "updated"]
    with core.db.session() as session:
        variant = session.get(Variant, ident)
        draft = session.get(RemoteDraft, ident)
        assert variant.revision == draft.synced_revision == 3
        assert draft.remote_hash == fingerprint({"body": variant.body})
        assert variant.body.endswith('Changed &quot;literal&quot;.')
        assert len(list(session.scalars(select(VariantRevision).where(VariantRevision.variant_id == ident)))) == 3


def test_postgres_read_media_gap_does_not_hide_local_edit(guarded_pg_hub, monkeypatch):
    core, _, after, _, records = core_stage(guarded_pg_hub[0], monkeypatch)
    ident = after["variant_id"]
    expected, target = sync_capture(core, ident)
    original_media = core.bridge._media
    def concurrent_local_edit(*args):
        result = original_media(*args)
        with core.db.session.begin() as session:
            session.get(Variant, ident).body += " Local concurrent edit."
        return result
    monkeypatch.setattr(core.bridge, "_media", concurrent_local_edit)
    with pytest.raises(AgentError, match="REMOTE_EDIT_CONFLICT"):
        core.bridge.import_post(records["fixture-post"], target, expected_local=expected)
    with core.db.session() as session:
        assert session.get(Variant, ident).body.endswith("Local concurrent edit.")
        assert session.get(RemoteDraft, ident).synced_revision == 2


def test_postgres_history_failure_rolls_back_plain_hash_and_synced_revision(guarded_pg_hub, monkeypatch):
    core, _, after, _, records = core_stage(guarded_pg_hub[0], monkeypatch)
    ident = after["variant_id"]
    expected, target = sync_capture(core, ident)
    remote = copy.deepcopy(records["fixture-post"])
    remote["content"] += " Changed."
    def fail_history(*args):
        raise RuntimeError("fixture-history-rollback")
    monkeypatch.setattr(core, "_history", fail_history)
    with pytest.raises(RuntimeError, match="fixture-history-rollback"):
        core.bridge.import_post(remote, target, expected_local=expected)
    with core.db.session() as session:
        variant = session.get(Variant, ident)
        assert core.snapshot(variant, session.get(Target, variant.target_id)) == after
        assert core.bridge._row_view(session.get(RemoteDraft, ident)) == expected["draft"]
        assert len(list(session.scalars(select(VariantRevision).where(VariantRevision.variant_id == ident)))) == 2
