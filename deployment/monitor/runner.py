"""Dedicated service-report process. Editorial VM access is pinned SSH and GET only."""

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.isolated_workspace import assert_isolated, mark_isolated
from max_postiz_agent.monitor_browser import MonitorBrowser
from max_postiz_agent.monitor_config import MonitorConfig, private_path
from max_postiz_agent.monitor_reports import _editorial, _routes, alert_signatures, render_report
from max_postiz_agent.monitor_service import MonitorService

AUTHORITY = "container_report_sender_v1"
HOLD = "report_only_sender_hold"
SCOPE = "pinned-editorial-monitor-v1"
MOSCOW = ZoneInfo("Europe/Moscow")
SOURCE_CONFIG = Path("/run/monitor/source.local.json")
SOURCE_SAMPLE = Path("/snapshot/source.json")


def require(condition, code):
    if not condition:
        raise AgentError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def clean_source_error():
    return {
        "schema": "monitor-snapshot-v1",
        "read_only": True,
        "collected_at": None,
        "editorial": _editorial({"available": False, "last_error": "MONITOR_STATE_UNAVAILABLE"}),
        "routes": _routes({"available": False, "error": "MONITOR_ROUTES_UNAVAILABLE"}),
    }


def accept_source(value, source, now):
    identity = value["source_identity"]
    (profile,) = source["profiles"]
    snap = value["snapshot"]
    require(
        value["contract"] == "remote-production-monitor-sample-v1"
        and value["binding"] == source["binding"] == SCOPE
        and value["status"] == "observed"
        and snap["read_only"] is True
        and snap["schema"] == "monitor-snapshot-v1"
        and identity["service"] == "max-postiz-editorial.service"
        and identity["root"] == "/opt/max-postiz"
        and all(
            identity[k] == profile[k]
            for k in ("version", "source_commit", "manifest_sha256", "pid", "start_ticks")
        )
        and type(identity["control_revision"]) is int
        and identity["control_revision"] >= source["minimum_revision"]
        and -15 <= now - snap["collected_at"] <= 120
        and snap["editorial"]["available"] is True
        and snap["editorial"]["version"] == profile["version"]
        and snap["routes"]["available"] is True,
        "MONITOR_SOURCE_PIN_MISMATCH",
    )
    return {**snap, "editorial": _editorial(snap["editorial"]), "routes": _routes(snap["routes"])}


class SnapshotSource:
    def __init__(self, config, snapshot):
        self.config = config
        self.snapshot = snapshot
        context_bytes = config.read_bytes()
        self.context = json.loads(context_bytes)
        self.context_sha256 = digest(context_bytes)
        self.last = None

    def read_snapshot(self):
        require(
            digest(self.config.read_bytes()) == self.context_sha256,
            "MONITOR_SOURCE_CONTEXT_CHANGED",
        )
        with self.snapshot.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, "MONITOR_SOURCE_SAMPLE_BOUND")
        value = json.loads(raw)
        require(
            value["context_sha256"] == self.context_sha256,
            "MONITOR_SOURCE_SAMPLE_CONTEXT_MISMATCH",
        )
        return accept_source(value["sample"], self.context["source"], time.time())

    def __call__(self, _core=None, *, now_at=None):
        try:
            self.last = self.read_snapshot()
        except (AgentError, ValueError, TypeError, KeyError, OSError):
            self.last = clean_source_error()
        return self.last


class Collector(SnapshotSource):
    def __init__(self, config, snapshot, private, sampler):
        super().__init__(config, snapshot)
        self.source = self.context["source"]
        host = self.context["host"]
        require(
            isinstance(host, str) and re.fullmatch(r"[0-9a-zA-Z][0-9a-zA-Z.-]{0,252}", host),
            "MONITOR_SOURCE_HOST_INVALID",
        )
        known = private / "known_hosts"
        key = private / "id_ed25519"
        self.known_hosts = known
        require(
            digest(known.read_bytes()) == self.context["known_hosts_sha256"] and key.is_file(),
            "MONITOR_SOURCE_KEY_PIN_REQUIRED",
        )
        self.argv = [
            "ssh",
            "-F",
            "/dev/null",
            "-T",
            "-i",
            str(key),
            "-o",
            f"UserKnownHostsFile={known}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            f"editorial@{host}",
            "/usr/bin/python3 -I -B -",
        ]
        self.input = f"REMOTE_CONTEXT = {self.source!r}\n".encode() + sampler.read_bytes()

    def collect(self):
        try:
            require(
                digest(self.config.read_bytes()) == self.context_sha256,
                "MONITOR_SOURCE_CONTEXT_CHANGED",
            )
            require(
                digest(self.known_hosts.read_bytes()) == self.context["known_hosts_sha256"],
                "MONITOR_SOURCE_KEY_PIN_REQUIRED",
            )
            result = subprocess.run(
                self.argv, input=self.input, capture_output=True, timeout=35, check=False
            )
            require(
                result.returncode == 0 and len(result.stdout) <= 1024 * 1024,
                "MONITOR_SOURCE_SSH_FAILED",
            )
            value = json.loads(result.stdout)
            accept_source(value, self.source, time.time())
        except (AgentError, ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
            value = None
        payload = json.dumps({"context_sha256": self.context_sha256, "sample": value}).encode()
        with tempfile.NamedTemporaryFile(dir=self.snapshot.parent, delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(payload)
                stream.close()
                temp.replace(self.snapshot)
            finally:
                temp.unlink(missing_ok=True)


def backup_snapshot(path):
    def standalone():
        require(
            not any(path.with_name(path.name + s).exists() for s in ("-wal", "-shm", "-journal")),
            "MONITOR_SEED_REQUIRES_STANDALONE_BACKUP",
        )

    standalone()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "MONITOR_SEED_INVALID")
        require(
            db.execute(
                "SELECT 1 FROM monitor_reports WHERE state IN "
                "('pending','sending','unknown') LIMIT 1"
            ).fetchone()
            is None,
            "MONITOR_SEED_HAS_OPEN_REPORTS",
        )
        require(
            db.execute("SELECT 1 FROM monitor_meta WHERE key='remote_startup_once'").fetchone()
            is not None,
            "MONITOR_SEED_SESSION_REQUIRED",
        )
    standalone()


def installed_seed(root):
    path = private_path(root, "data/monitor/monitor.sqlite")
    require(path.is_file(), "MONITOR_STATE_NOT_INITIALIZED")
    with path.open("rb") as stream:
        require(stream.read(16) == b"SQLite format 3\x00", "MONITOR_STATE_NOT_INITIALIZED")


def init_state(root, seed, expected_hash):
    raw = seed.read_bytes()
    require(digest(raw) == expected_hash, "MONITOR_SEED_HASH_CHANGED")
    backup_snapshot(seed)
    require(seed.read_bytes() == raw, "MONITOR_SEED_HASH_CHANGED")
    path = private_path(root, "data/monitor/monitor.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)
    with profile_lock(private_path(root, "data/monitor/init.lock")):
        if path.exists():
            raise FileExistsError(path)
        mark_isolated(root, Path("/editorial-source-not-mounted"))
        config = load_config(root)
        require(config.value["enabled"], "MONITOR_CONFIG_DISABLED")
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
            temp = Path(out.name)
            try:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
                out.close()
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)


def load_config(root):
    config = MonitorConfig.load(root)
    commands = config.value["commands"]
    require(
        config.value["channel"] == "chromium"
        and config.value["headless"] is True
        and config.value["report_private_confirmed"] is True
        and not commands["enabled"]
        and not commands["reply_in_control_chat"],
        "MONITOR_REPORT_ONLY_CONFIG_REQUIRED",
    )
    return config


def require_idle(store):
    with store.connection() as db:
        require(
            db.execute(
                "SELECT 1 FROM monitor_reports WHERE state IN "
                "('pending','sending','unknown') LIMIT 1"
            ).fetchone()
            is None,
            "MONITOR_OUTBOX_REQUIRES_RECONCILIATION",
        )


def state_hash(config, collector):
    return fingerprint({"config": config.hash, "source": collector.context})


def arm(service, collector, transport):
    store = service.store
    require_idle(store)
    require(store.meta(AUTHORITY) is None, "MONITOR_AUTHORITY_ALREADY_SET")
    require(transport.inspect()["publish"] is True, "MONITOR_CHANNEL_ACCESS_REQUIRED")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")

        def save(key, value):
            db.execute(
                "INSERT INTO monitor_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

        require(
            db.execute("SELECT 1 FROM monitor_meta WHERE key=?", (AUTHORITY,)).fetchone() is None,
            "MONITOR_AUTHORITY_ALREADY_SET",
        )
        require(
            db.execute(
                "SELECT 1 FROM monitor_reports WHERE state IN "
                "('pending','sending','unknown') LIMIT 1"
            ).fetchone()
            is None,
            "MONITOR_OUTBOX_REQUIRES_RECONCILIATION",
        )
        before = dict(
            db.execute("SELECT key,value FROM monitor_meta WHERE key IN (?,?)", ("sender", HOLD))
        )
        save("monitor_container_predecessor_v1", before)
        save(HOLD, False)
        save(
            AUTHORITY,
            {"state": "armed", "binding": state_hash(service.config, collector), "at": time.time()},
        )


def rebind(service, collector, transport, expected):
    store = service.store
    authority = store.meta(AUTHORITY, {})
    binding = state_hash(service.config, collector)
    require(
        authority.get("state") == "verified"
        and authority.get("binding") == expected
        and expected != binding,
        "MONITOR_REBIND_PREDECESSOR_MISMATCH",
    )
    report = store.get(authority["report_id"])
    require(
        report is not None
        and report["state"] == "confirmed"
        and report["config_hash"] == service.config.hash,
        "MONITOR_REBIND_DESTINATION_MISMATCH",
    )
    require(transport.inspect()["publish"] is True, "MONITOR_CHANNEL_ACCESS_REQUIRED")
    snapshot = service.snapshot()
    require(
        snapshot["editorial"]["available"] and snapshot["routes"]["available"],
        "MONITOR_REBIND_SOURCE_UNAVAILABLE",
    )
    at = time.time()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        values = dict(
            db.execute(
                "SELECT key,value FROM monitor_meta WHERE key IN (?,?,?)",
                (AUTHORITY, HOLD, "sender"),
            )
        )
        require(
            json.loads(values.get(AUTHORITY, "null")) == authority
            and json.loads(values.get(HOLD, "true")) is False
            and json.loads(values.get("sender", "{}")).get("state") in {"confirmed", "idle"}
            and db.execute(
                "SELECT 1 FROM monitor_reports WHERE state IN "
                "('pending','sending','unknown') LIMIT 1"
            ).fetchone()
            is None,
            "MONITOR_OUTBOX_REQUIRES_RECONCILIATION",
        )
        audit = {"before": authority, "binding": binding, "at": at}
        db.execute(
            "INSERT INTO monitor_meta(key,value) VALUES (?,?)",
            ("monitor_container_rebind_v1:" + fingerprint(audit), json.dumps(audit)),
        )
        db.execute(
            "UPDATE monitor_meta SET value=? WHERE key=?",
            (json.dumps({**authority, "binding": binding, "rebound_at": at}), AUTHORITY),
        )
    return {"state": "rebound", "binding": binding}


def wait_report_receipt(page, state):
    from max_postiz_agent import browser_worker

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        matches = browser_worker._matched_posts(page, state["text"], illustrated=False)
        if matches.count() > state["before_count"] + 1:
            return None
        result = browser_worker.receipt(page, state)
        if result:
            return result
        page.wait_for_timeout(200)
    return None


class ServiceChannel(MonitorBrowser):
    def send_report(self, chat_id, text, request_id, *, authorize):
        from max_postiz_agent import browser_worker

        old = browser_worker._loaded_receipt
        browser_worker._loaded_receipt = wait_report_receipt
        try:
            return super().send_report(chat_id, text, request_id, authorize=authorize)
        finally:
            browser_worker._loaded_receipt = old


def exact_readback(transport, report):
    from max_postiz_agent import browser_worker
    from max_postiz_agent.browser_runtime import ensure_channel, session

    worker_id = transport._request_id(report["id"])
    path = browser_worker.ledger_path(transport.settings, worker_id)
    raw = path.read_bytes()
    saved = json.loads(raw)
    require(
        saved["request_id"] == worker_id
        and saved["chat_id"] == report["chat_id"]
        and saved["text"] == report["text"]
        and saved["send_started"] is True
        and saved["before_count"] == 0
        and saved.get("request_attachment") is None
        and saved.get("attachment") is False
        and saved["body_sha256"] == digest(report["text"].encode())
        and saved["state"] == "confirmed",
        "MONITOR_REPORT_JOURNAL_NOT_VERIFIED",
    )
    with session(transport.settings) as page:
        ensure_channel(page, transport.settings, report["chat_id"], require_publish=False)
        proof = wait_report_receipt(page, saved)
    require(proof and path.read_bytes() == raw, "MONITOR_INDEPENDENT_READBACK_FAILED")
    return digest(raw)


def trial(service, collector, transport, root):
    store = service.store
    authority = store.meta(AUTHORITY, {})
    require(
        authority.get("state") == "armed"
        and authority["binding"] == state_hash(service.config, collector),
        "MONITOR_TRIAL_NOT_ARMED",
    )
    require_idle(store)
    snap = service.snapshot()
    require(
        snap["editorial"]["available"] and snap["routes"]["available"],
        "MONITOR_TRIAL_SOURCE_UNAVAILABLE",
    )
    key = "omv-service-cutover:" + authority["binding"][:20]
    authority["state"] = "trial_started"
    store.set_meta(AUTHORITY, authority)
    stamp = datetime.fromtimestamp(snap["collected_at"], MOSCOW).strftime("%d.%m.%Y %H:%M:%S МСК")
    text = f"Служебный отчёт мониторинга · {stamp}\n" + render_report(
        snap, report_id=authority["binding"][:12], kind="status"
    )
    row = service.queue(key, "incident", text[:3900])
    authority["report_id"] = row["id"]
    store.set_meta(AUTHORITY, authority)
    store.set_meta(HOLD, True)
    result = service.deliver_once()
    require(
        result.get("id") == row["id"] and result["state"] == "confirmed",
        "MONITOR_TRIAL_REQUIRES_RECONCILIATION",
    )
    with profile_lock(private_path(root, "data/monitor/transport.lock")):
        proof = exact_readback(transport, row)
    require_idle(store)
    store.set_meta("incidents", alert_signatures(snap))
    seen = historical_publications(store)
    seen.update(x["id"] for x in snap["routes"]["dispatches"] if x["state"] == "published")
    store.set_meta("monitor_container_seen_dispatches", sorted(seen))
    authority.update(state="verified", proof_sha256=proof, verified_at=time.time())
    store.set_meta(HOLD, False)
    store.set_meta(AUTHORITY, authority)
    return {"state": "verified", "report_id": row["id"], "browser_journal_sha256": proof}


def historical_publications(store):
    seen = set(store.meta("report_watch_published_v1", []))
    with store.connection() as db:
        for (key,) in db.execute(
            "SELECT event_key FROM monitor_reports WHERE event_key LIKE 'native-max-confirmed:%'"
        ):
            seen.add(key.split(":", 1)[1].split("@", 1)[0])
    return seen


def publication_alerts(service, collector):
    snapshot = collector.last
    if not snapshot["routes"]["available"]:
        return
    seen = set(service.store.meta("monitor_container_seen_dispatches", []))
    added = set()
    for row in snapshot["routes"]["dispatches"]:
        if (
            row["id"]
            and row["id"] not in seen
            and row["platform"] == "max"
            and row["state"] == "published"
            and row["verified"]
            and row["evidence_kind"] == "independent_readback"
            and row["attempts"] == 1
        ):
            service.queue(
                "native-max-confirmed:" + row["id"],
                "incident",
                "MAX: редакционная VM подтвердила публикацию одной попыткой.\n"
                f"Готовых статей в резерве: {snapshot['editorial']['reserve']['ready']}.",
            )
            added.add(row["id"])
    if added:
        service.store.set_meta("monitor_container_seen_dispatches", sorted(seen | added))


def stop_event():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def run(service, collector, *, sender):
    stop = stop_event()
    binding = state_hash(service.config, collector)
    previous = None
    while not stop.is_set():
        authority = service.store.meta(AUTHORITY, {})
        if sender and authority.get("state") == "verified" and authority.get("binding") == binding:
            result = service.report_only_tick(
                stop_requested=stop.is_set,
                after_produce=lambda: publication_alerts(service, collector),
            )
            state = result["state"]
        else:
            service.observe()
            state = "observing"
        if state != previous:
            print(json.dumps({"state": state}), flush=True)
            previous = state
        stop.wait(service.config.value["poll_seconds"])


def service_settings(root):
    return Settings(
        root,
        {
            "state_dir": "data/state",
            "media_dir": "data/media",
            "logs_dir": "logs",
            "max": {"adapter_mode": "browser"},
        },
        {},
        {},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "init",
            "collect",
            "preview",
            "observe",
            "login",
            "inspect",
            "arm",
            "trial",
            "rebind",
            "run",
        ),
    )
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--seed-sha256")
    parser.add_argument("--expected-binding")
    args = parser.parse_args()
    os.umask(0o077)
    root = Path("/state")
    if args.mode == "init":
        require(args.seed is not None and args.seed_sha256 is not None, "MONITOR_SEED_REQUIRED")
        init_state(root, args.seed, args.seed_sha256)
        return
    if args.mode == "collect":
        source = Collector(
            SOURCE_CONFIG,
            SOURCE_SAMPLE,
            Path("/run/monitor-ssh"),
            Path(__file__).with_name("source_sampler.py"),
        )
        stop = stop_event()
        while not stop.is_set():
            source.collect()
            stop.wait(30)
        return
    assert_isolated(root)
    installed_seed(root)
    config = load_config(root)
    collector = SnapshotSource(SOURCE_CONFIG, SOURCE_SAMPLE)
    if args.mode == "preview":
        snap = collector()
        require(snap["editorial"]["available"], "MONITOR_SOURCE_UNAVAILABLE")
        print(
            json.dumps(
                {
                    "state": "observed",
                    "version": snap["editorial"]["version"],
                    "ready": snap["editorial"]["reserve"]["ready"],
                    "heartbeat_at": snap["editorial"]["heartbeat_at"],
                }
            )
        )
        return
    settings = service_settings(root)
    core = SimpleNamespace(settings=settings)
    transport = ServiceChannel(settings, config.browser_options())
    with profile_lock(private_path(root, "data/monitor/remote-report-only.lock")):
        service = MonitorService(core, config, collector=collector, transport=transport)
        if args.mode in {"login", "inspect", "arm", "rebind"}:
            with profile_lock(private_path(root, "data/monitor/transport.lock")):
                if args.mode == "login":
                    transport.login(timeout=900)
                elif args.mode == "inspect":
                    print(json.dumps({"publish": transport.inspect()["publish"]}))
                elif args.mode == "arm":
                    arm(service, collector, transport)
                else:
                    require(args.expected_binding, "MONITOR_REBIND_PREDECESSOR_REQUIRED")
                    print(json.dumps(rebind(service, collector, transport, args.expected_binding)))
        elif args.mode == "trial":
            print(json.dumps(trial(service, collector, transport, root)))
        else:
            run(service, collector, sender=args.mode == "run")


if __name__ == "__main__":
    try:
        main()
    except AgentError as error:
        logging.error("%s", error.code)
        raise SystemExit(2) from None
