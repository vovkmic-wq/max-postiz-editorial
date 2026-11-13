from contextlib import closing
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import time
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.monitor_config import MonitorConfig, default_config
from max_postiz_agent.monitor_service import MonitorService
from max_postiz_agent.monitor_store import MonitorStore

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deployment/monitor/runner.py"))


@pytest.fixture
def copy_monitor(tmp_path, monkeypatch):
    # This fixture uses SQLite; an unrelated PostgreSQL test URL must not be
    # mistaken for a connection configured for the isolated monitor runtime.
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    root = tmp_path / "monitor"
    root.mkdir()
    mark_isolated(root, tmp_path / "source")
    (root / "config").mkdir()
    value = default_config()
    value.update(
        enabled=True,
        report_private_confirmed=True,
        report_chat_id="-123",
        report_chat_name="Test reports",
        channel="chromium",
        hourly_enabled=False,
        daily_enabled=False,
    )
    (root / "config/monitor.json").write_text(json.dumps(value), encoding="utf-8")
    config = MonitorConfig.load(root)
    return root, config


def test_report_seed_preserves_history_and_refuses_open_reports(copy_monitor):
    root, config = copy_monitor
    store = MonitorStore(root)
    store.set_meta("remote_startup_once", {"report_id": "history"})
    report = store.enqueue("event", "incident", "history", config.hash, "-123", at=1, expires_at=99)
    with pytest.raises(AgentError, match="MONITOR_SEED_HAS_OPEN_REPORTS"):
        RUNNER["backup_snapshot"](store.path)
    claim = store.claim(config.hash, "-123", at=2)
    assert claim["id"] == report["id"]
    store.finish(claim, "confirmed", {}, at=3)
    raw = store.path.read_bytes()
    RUNNER["backup_snapshot"](store.path)
    assert store.path.read_bytes() == raw


@pytest.mark.parametrize("native_state", ["confirmed", "unknown"])
def test_container_trial_is_single_use(copy_monitor, monkeypatch, native_state):
    root, config = copy_monitor
    store = MonitorStore(root)
    collector = SimpleNamespace(context={"source": "test"})
    snapshot = {
        "schema": "monitor-snapshot-v1",
        "read_only": True,
        "collected_at": 1000,
        "editorial": {"available": True, "version": "0.14.5", "heartbeat_at": 999},
        "routes": {"available": True, "dispatches": []},
    }
    calls = []

    class Transport:
        def inspect(self):
            return {"publish": True}

        def send_report(self, chat_id, text, request_id, *, authorize):
            assert authorize(
                {
                    "request_id": request_id,
                    "chat_id": chat_id,
                    "text": text,
                    "chat_name": "Test reports",
                    "destination_kind": "channel",
                }
            )
            calls.append(request_id)
            return {
                "state": native_state,
                "verification": "browser_readback",
                "independent_readback": True,
            }

    settings = Settings(
        root,
        {"state_dir": "data/state", "media_dir": "data/media", "logs_dir": "logs", "max": {}},
        {},
        {},
    )
    transport = Transport()
    service = MonitorService(
        SimpleNamespace(settings=settings),
        config,
        store=store,
        transport=transport,
        collector=lambda _core, *, now_at: snapshot,
        clock=lambda: 1000,
    )
    store.set_meta("report_watch_published_v1", ["older-dispatch"])
    old = store.enqueue(
        "native-max-confirmed:archived-dispatch@old-config",
        "incident",
        "history",
        config.hash,
        "-123",
        at=1,
        expires_at=99,
    )
    claim = store.claim(config.hash, "-123", at=2)
    assert claim["id"] == old["id"]
    store.finish(claim, "confirmed", {}, at=3)
    store.set_meta("report_only_sender_hold", True)
    store.set_meta("sender", {"state": "failed", "heartbeat_at": 1})
    RUNNER["arm"](service, collector, transport)
    archived = store.meta("monitor_container_predecessor_v1")
    assert json.loads(archived["report_only_sender_hold"]) is True
    trial = RUNNER["trial"]
    monkeypatch.setitem(trial.__globals__, "exact_readback", lambda *_: "proof")
    if native_state == "confirmed":
        assert trial(service, collector, transport, root)["state"] == "verified"
        assert store.meta("monitor_container_seen_dispatches") == [
            "archived-dispatch",
            "older-dispatch",
        ]
    else:
        with pytest.raises(AgentError, match="MONITOR_TRIAL_REQUIRES_RECONCILIATION"):
            trial(service, collector, transport, root)
        assert store.meta("report_only_sender_hold") is True and store.unresolved()
    with pytest.raises(AgentError, match="MONITOR_TRIAL_NOT_ARMED"):
        trial(service, collector, transport, root)
    assert len(calls) == 1


def test_first_start_imports_seed_into_empty_volume(copy_monitor, tmp_path):
    root, config = copy_monitor
    store = MonitorStore(root)
    store.set_meta("remote_startup_once", {"report_id": "history"})
    report = store.enqueue("event", "incident", "history", config.hash, "-123", at=1, expires_at=99)
    claim = store.claim(config.hash, "-123", at=2)
    store.finish(claim, "confirmed", {}, at=3)
    target = tmp_path / "first-install"
    (target / "config").mkdir(parents=True)
    (target / "config/monitor.json").write_bytes((root / "config/monitor.json").read_bytes())
    raw = store.path.read_bytes()
    RUNNER["init_state"](target, store.path, hashlib.sha256(raw).hexdigest())
    installed = MonitorStore(target)
    assert installed.get(report["id"]) == store.get(report["id"])
    assert installed.meta("remote_startup_once") == store.meta("remote_startup_once")
    with pytest.raises(FileExistsError):
        RUNNER["init_state"](target, store.path, hashlib.sha256(raw).hexdigest())


def test_first_launch_builds_native_monitor_transport(copy_monitor):
    root, config = copy_monitor
    transport = RUNNER["ServiceChannel"](RUNNER["service_settings"](root), config.browser_options())
    assert transport.report_chat_id == "-123"
    assert transport.settings.app["max"]["browser"]["channel"] == "chromium"


def test_seed_import_refuses_wal_and_interrupted_destination(copy_monitor):
    root, _ = copy_monitor
    store = MonitorStore(root)
    store.set_meta("remote_startup_once", {"report_id": "history"})
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO monitor_meta VALUES ('new_history','true')")
        conn.commit()
        with pytest.raises(AgentError, match="MONITOR_SEED_REQUIRES_STANDALONE_BACKUP"):
            RUNNER["backup_snapshot"](store.path)
    store.path.rename(store.path.with_suffix(".backup"))
    with pytest.raises(AgentError, match="MONITOR_STATE_NOT_INITIALIZED"):
        RUNNER["installed_seed"](root)
    store.path.touch()
    with pytest.raises(AgentError, match="MONITOR_STATE_NOT_INITIALIZED"):
        RUNNER["installed_seed"](root)


def test_snapshot_source_reports_unknown_after_stale_or_wrong_binding(tmp_path):
    config = tmp_path / "source.local.json"
    sample = tmp_path / "sample.json"
    profile = {
        "version": "0.14.5",
        "source_commit": "a",
        "manifest_sha256": "b",
        "pid": 123,
        "start_ticks": 456,
    }
    config.write_text(
        json.dumps(
            {"source": {"binding": RUNNER["SCOPE"], "profiles": [profile], "minimum_revision": 1}}
        ),
        encoding="utf-8",
    )
    source = RUNNER["SnapshotSource"](config, sample)
    value = {
        "context_sha256": source.context_sha256,
        "sample": {
            "contract": "remote-production-monitor-sample-v1",
            "binding": RUNNER["SCOPE"],
            "status": "observed",
            "source_identity": {
                **profile,
                "service": "max-postiz-editorial.service",
                "root": "/opt/max-postiz",
                "control_revision": 1,
            },
            "snapshot": {
                "schema": "monitor-snapshot-v1",
                "read_only": True,
                "collected_at": time.time(),
                "editorial": {"available": True, "version": profile["version"], "paused": True},
                "routes": {"available": True},
            },
        },
    }
    sample.write_text(json.dumps(value), encoding="utf-8")
    assert source()["editorial"]["paused"] is True
    value["sample"]["snapshot"]["collected_at"] -= 180
    sample.write_text(json.dumps(value), encoding="utf-8")
    assert source()["collected_at"] is None
    assert source.last["editorial"]["available"] is False
    assert source.last["editorial"]["paused"] is False
    value["sample"]["snapshot"]["collected_at"] = time.time()
    value["context_sha256"] = "different"
    sample.write_text(json.dumps(value), encoding="utf-8")
    assert source()["collected_at"] is None


def test_collector_writes_unavailable_snapshot_without_sudo(tmp_path, monkeypatch):
    pins = tmp_path / "known_hosts"
    pins.write_bytes(b"test host key")
    (tmp_path / "id_ed25519").touch()
    config = tmp_path / "source.local.json"
    config.write_text(
        json.dumps(
            {
                "host": "editorial.test",
                "source": {},
                "known_hosts_sha256": hashlib.sha256(pins.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    sampler = tmp_path / "sampler.py"
    sampler.write_text("# test sampler\n", encoding="utf-8")
    sample = tmp_path / "sample.json"
    source = RUNNER["Collector"](config, sample, tmp_path, sampler)
    calls = []

    def unavailable(argv, **kwargs):
        calls.append(argv[-1])
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr(RUNNER["subprocess"], "run", unavailable)
    source.collect()
    assert calls == ["/usr/bin/python3 -I -B -"]
    assert json.loads(sample.read_bytes()) == {
        "context_sha256": source.context_sha256,
        "sample": None,
    }
    pins.write_bytes(b"changed host key")
    source.collect()
    assert len(calls) == 1


def test_rebind_preserves_confirmed_report_and_refuses_open_outbox(copy_monitor):
    root, config = copy_monitor
    store = MonitorStore(root)
    report = store.enqueue("event", "incident", "history", config.hash, "-123", at=1, expires_at=99)
    claim = store.claim(config.hash, "-123", at=2)
    store.finish(claim, "confirmed", {}, at=3)
    before = {"state": "verified", "binding": "previous", "report_id": report["id"]}
    store.set_meta(RUNNER["AUTHORITY"], before)
    store.set_meta(RUNNER["HOLD"], False)
    store.set_meta("sender", {"state": "confirmed"})
    service = SimpleNamespace(
        store=store,
        config=config,
        snapshot=lambda: {
            "editorial": {"available": True},
            "routes": {"available": True},
        },
    )
    collector = SimpleNamespace(context={"new": True})
    transport = SimpleNamespace(inspect=lambda: {"publish": True})
    rebind = RUNNER["rebind"]
    queued = store.enqueue("next", "incident", "test", config.hash, "-123", at=4, expires_at=99)
    with pytest.raises(AgentError, match="MONITOR_OUTBOX_REQUIRES_RECONCILIATION"):
        rebind(service, collector, transport, "previous")
    assert store.get(queued["id"]) == queued
    assert store.meta(RUNNER["AUTHORITY"]) == before
    claim = store.claim(config.hash, "-123", at=5)
    store.finish(claim, "confirmed", {}, at=6)
    old_history = store.recent()
    result = rebind(service, collector, transport, "previous")
    assert store.meta(RUNNER["AUTHORITY"])["report_id"] == report["id"]
    assert store.meta(RUNNER["AUTHORITY"])["binding"] == result["binding"]
    assert store.recent() == old_history
    with pytest.raises(AgentError, match="MONITOR_REBIND_PREDECESSOR_MISMATCH"):
        rebind(service, collector, transport, "previous")


def test_publication_alerts_require_independent_evidence(copy_monitor):
    root, _ = copy_monitor
    store = MonitorStore(root)
    queued = []
    service = SimpleNamespace(store=store, queue=lambda *args: queued.append(args))
    common = {"platform": "max", "state": "published", "verified": True, "attempts": 1}
    collector = SimpleNamespace(
        last={
            "editorial": {"reserve": {"ready": 5}},
            "routes": {
                "available": True,
                "dispatches": [
                    {**common, "id": "legacy", "evidence_kind": "api_receipt"},
                    {**common, "id": "verified", "evidence_kind": "independent_readback"},
                ],
            },
        }
    )
    for _ in range(2):
        RUNNER["publication_alerts"](service, collector)
    assert [row[0] for row in queued] == ["native-max-confirmed:verified"]
