"""Retirement preserves unknown model history; no live inference in these tests."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from max_postiz_agent import translation_retirement as recovery
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Base
from max_postiz_agent.hub_models import Target
from max_postiz_agent.server_models import (
    EditorialControl,
    EditorialLedger,
    EditorialPreparationClaim,
)


def make_case(tmp_path, engine, monkeypatch):
    Base.metadata.create_all(engine)
    core = SimpleNamespace(
        settings=SimpleNamespace(root=Path(tmp_path)),
        db=SimpleNamespace(engine=engine, session=sessionmaker(engine, expire_on_commit=False)),
    )
    claim_id, record_id, token = (str(uuid4()) for _ in range(3))
    claim = EditorialPreparationClaim(
        id=claim_id,
        token=token,
        control_revision=7,
        record_id=record_id,
        active_key="models",
        state="blocked_unknown",
        error=recovery.ERROR,
        created_at=1000.0,
        updated_at=1010.0,
    )
    article = {
        "title": "A star",
        "original": "A star in space.",
        "url": "https://example.org/article",
    }
    record = {
        "id": record_id,
        "state": "needs_attention",
        "error": recovery.ERROR,
        "slot": 10,
        "url": article["url"],
        "source_id": "test",
        "target_id": "max",
        "original_hash": "1" * 64,
        "policy_hash": "2" * 64,
        "source_published_at": 900.0,
        "reserve_contract": "article-reserve-v1",
        "input_hash": fingerprint(article),
        "reserve_claim": {"id": claim_id, "token": token, "control_revision": 7, "record_id": None},
        "quality_trace": f"data/hourly-articles/quality-{record_id}.local.jsonl",
        "input_file": f"data/hourly-articles/input-{record_id}.local.json",
        "preparation_attempts": [
            {
                "number": 1,
                "state": "needs_attention",
                "error": recovery.ERROR,
                "operator_retry": False,
                "started_at": 1001.0,
                "finished_at": 1009.0,
            }
        ],
    }
    binding = {
        k: record.get(k)
        for k in (
            "id",
            "reserve_contract",
            "reserve_claim",
            "input_hash",
            "policy_hash",
            "target_id",
            "source_id",
            "original_hash",
            "source_published_at",
        )
    }
    record["reserve_binding_hash"] = fingerprint(binding)

    def save(relative, value, lines=False):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ("\n".join(json.dumps(x) for x in value) if lines else json.dumps(value)).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    refs = {
        "input": save(record["input_file"], article),
        "trace": save(
            record["quality_trace"],
            [
                {
                    "phase": "translation_started",
                    "attempt": 1,
                    "at": 1002.0,
                    "record_id": record_id,
                    "context": {
                        "source_untrusted": {
                            "title": article["title"],
                            "body": article["original"],
                            "url": article["url"],
                        }
                    },
                },
                {
                    "phase": "translation_failed",
                    "attempt": 1,
                    "at": 1008.0,
                    "record_id": record_id,
                    "error": recovery.ERROR,
                },
            ],
            True,
        ),
    }
    record["input_file_hash"] = refs["input"]["sha256"]
    wrapper = str(tmp_path / "deployment/linux/madlad-ssh-stdio.sh")
    messages = [
        f'socat[42] E execvp("{wrapper}", "{wrapper}"): Permission denied',
        "socat[41] W waitpid(): child 42 exited with status 1",
        "socat[41] E read(5, 0xabcd, 8192): Connection reset by peer",
    ]
    journal = [
        {
            "__REALTIME_TIMESTAMP": str(1003000000 + i),
            "_SYSTEMD_UNIT": "max-postiz-madlad-tunnel.service",
            "_BOOT_ID": "boot",
            "_PID": "42" if i == 0 else "41",
            "MESSAGE": m,
        }
        for i, m in enumerate(messages)
    ]
    refs["journal"] = save("data/journal.jsonl", journal, True)
    refs["health"] = save(
        "data/health.json",
        {
            "at": 1020.0,
            "http_status": 200,
            "generation_requests": 0,
            "body": {
                "status": "configured",
                "artifacts_verified": True,
                "backend_contract": "madlad400-candle-causal-v2",
                "model": "madlad400-3b-q4k-causal-v2",
                "busy": False,
                "quarantined": False,
            },
        },
    )
    with core.db.session.begin() as s:
        s.add(EditorialControl(id="main", paused=True, revision=8, settings={}, runtime={}))
        s.add(claim)
        s.add(
            EditorialLedger(
                id="hourly",
                revision=4,
                payload={
                    "records": [
                        copy.deepcopy(record),
                        {"id": "historical", "state": "cancelled", "error": "RETAINED"},
                    ]
                },
            )
        )
        s.add(
            Target(
                id="max",
                platform="max",
                profile_id="test",
                route="max_browser",
                paused=True,
                options={},
                binding_hash="0" * 64,
            )
        )
        s.add(
            Audit(
                id=str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record_id)),
                actor="article_reserve",
                action="article_reserve_preparation_binding",
                entity=record_id,
                details={"binding": binding, "binding_hash": fingerprint(binding)},
            )
        )
    seal = {
        "contract": recovery.CONTRACT,
        "claim_hash": fingerprint(recovery._view(claim)),
        "record_hash": fingerprint(record),
        "owner_decision": "retire_source_preserve_unknown_no_retry",
        "boot_id": "boot",
        "files": refs,
    }
    args = {
        "operation_id": str(uuid4()),
        "expected_revision": 8,
        "actor": "owner_maintenance:test",
        "expected_claim_hash": seal["claim_hash"],
        "expected_record_hash": seal["record_hash"],
        "proof": save("data/proof.json", seal),
    }
    monkeypatch.setattr(recovery, "now", lambda: 1030.0)
    return SimpleNamespace(
        core=core,
        claim_id=claim_id,
        record_id=record_id,
        args=args,
        save=save,
        seal=seal,
        record=record,
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    try:
        yield make_case(tmp_path, engine, monkeypatch)
    finally:
        engine.dispose()


def snapshot(c):
    with c.core.db.session() as s:
        return {
            t.name: [dict(r) for r in s.execute(t.select()).mappings()]
            for t in Base.metadata.sorted_tables
        }


def apply(c):
    return recovery.apply(c.core, c.claim_id, **c.args)


def check_commit(c):
    before = snapshot(c)
    receipt = apply(c)
    after = snapshot(c)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"editorial_article_ledger", "editorial_preparation_claims", "audit_events"}
    with c.core.db.session() as s:
        claim = s.get(EditorialPreparationClaim, c.claim_id)
        record, historical = s.get(EditorialLedger, "hourly").payload["records"]
        assert (
            claim.state == "abandoned_unknown"
            and claim.active_key is None
            and claim.error == recovery.ERROR
        )
        assert record["state"] == "cancelled" and historical == {
            "id": "historical",
            "state": "cancelled",
            "error": "RETAINED",
        }
        assert {
            k: v
            for k, v in record.items()
            if k not in {"state", "cancelled_at", "maintenance_disposition"}
        } == {k: v for k, v in c.record.items() if k != "state"}
    assert receipt["historical_model_outcome"] == "unknown"
    Path(c.args["proof"]["path"]).unlink()
    assert apply(c) == recovery.status(c.core, c.args["operation_id"]) == receipt
    assert snapshot(c) == after


def test_atomic_retirement_and_response_loss_replay(case):
    check_commit(case)


@pytest.mark.parametrize(
    "damage",
    [
        "revision",
        "unpaused",
        "claim_hash",
        "record_hash",
        "bound_token",
        "staged",
        "trace",
        "journal",
        "health",
        "binding",
    ],
)
def test_refuses_changed_or_unsafe_evidence(case, damage):
    c = case
    if damage in {"revision", "unpaused", "bound_token", "staged", "binding"}:
        with c.core.db.session.begin() as s:
            if damage == "revision":
                s.get(EditorialControl, "main").revision += 1
            elif damage == "unpaused":
                s.get(EditorialControl, "main").paused = False
            elif damage == "bound_token":
                s.get(EditorialPreparationClaim, c.claim_id).token = str(uuid4())
            elif damage == "binding":
                s.scalar(select(Audit)).details = {}
            else:
                ledger = s.get(EditorialLedger, "hourly")
                payload = copy.deepcopy(ledger.payload)
                payload["records"][0]["postiz_id"] = "staged"
                ledger.payload = payload
                c.args["expected_record_hash"] = fingerprint(payload["records"][0])
    elif damage.endswith("hash"):
        c.args["expected_" + damage] = "0" * 64
    else:
        path = Path(c.seal["files"][damage]["path"])
        path.write_bytes(b"{}")
    before = snapshot(c)
    with pytest.raises((AgentError, ValueError, KeyError)):
        apply(c)
    assert snapshot(c) == before


def check_rollback(c):
    before = snapshot(c)

    def fail(mapper, connection, target):
        if target.action == recovery.ACTION:
            raise RuntimeError("audit unavailable")

    event.listen(Audit, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError):
            apply(c)
    finally:
        event.remove(Audit, "before_insert", fail)
    assert snapshot(c) == before


def test_audit_failure_rolls_back_both_rows(case):
    check_rollback(case)


@pytest.mark.parametrize(
    "damage", ["busy", "stale", "backend", "unit", "pid", "time", "extra_trace", "source"]
)
def test_owner_resealed_but_semantically_unsafe_proof_is_refused(case, damage):
    c = case
    name = (
        "health"
        if damage in {"busy", "stale", "backend"}
        else ("trace" if damage in {"extra_trace", "source"} else "journal")
    )
    ref = c.seal["files"][name]
    path = Path(ref["path"])
    value = (
        json.loads(path.read_bytes())
        if name == "health"
        else [json.loads(x) for x in path.read_bytes().splitlines()]
    )
    if damage == "busy":
        value["body"]["busy"] = True
    elif damage == "stale":
        value["at"] = 1
    elif damage == "backend":
        value["body"]["backend_contract"] = "wrong"
    elif damage == "unit":
        value[0]["_SYSTEMD_UNIT"] = "other.service"
    elif damage == "pid":
        value[1]["_PID"] = "42"
    elif damage == "time":
        value[0]["__REALTIME_TIMESTAMP"] = "2000000000"
    elif damage == "source":
        value[0]["context"]["source_untrusted"]["body"] = "A different source"
    else:
        value.append(
            {"phase": "translation_returned", "record_id": c.record_id, "attempt": 1, "at": 1009}
        )
    c.seal["files"][name] = c.save(
        str(path.relative_to(c.core.settings.root)), value, name != "health"
    )
    c.args["proof"] = c.save("data/proof.json", c.seal)
    before = snapshot(c)
    with pytest.raises(AgentError):
        apply(c)
    assert snapshot(c) == before
