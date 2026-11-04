"""Synthetic protocol tests; actual operational identifiers/proofs stay private."""

import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from max_postiz_agent import unbound_selection_reconciliation as recovery
from max_postiz_agent.article_reserve import ArticleReserveStore
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Base
from max_postiz_agent.hub_models import RecurringTask, Target
from max_postiz_agent.server_models import (
    EditorialControl,
    EditorialLedger,
    EditorialPreparationClaim,
)


def snapshot(case):
    with case.core.db.session() as session:
        return {
            t.name: sorted([dict(r) for r in session.execute(t.select()).mappings()], key=repr)
            for t in Base.metadata.sorted_tables
        }


def make_case(tmp_path, engine, monkeypatch):
    Base.metadata.create_all(engine)
    calls = []

    class Core:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("External capability forbidden")

    core = Core()
    core.db = SimpleNamespace(
        engine=engine, session=sessionmaker(bind=engine, expire_on_commit=False)
    )
    core.settings = SimpleNamespace(
        root=Path(tmp_path), hash=fingerprint({"synthetic_settings": True})
    )
    claim_id, token = str(uuid4()), str(uuid4())
    with core.db.session.begin() as session:
        session.add(
            EditorialControl(
                id="main", paused=True, revision=8, settings={"source": "open_science"}, runtime={}
            )
        )
        session.add(
            EditorialPreparationClaim(
                id=claim_id,
                token=token,
                control_revision=7,
                active_key="models",
                state="blocked_unknown",
                error=recovery.OLD_ERROR,
                created_at=1000.0,
                updated_at=1009.0,
            )
        )
        session.add(
            EditorialPreparationClaim(
                id=str(uuid4()),
                token=str(uuid4()),
                control_revision=2,
                state="abandoned_unknown",
                error="RETAINED_HISTORICAL_UNKNOWN",
                created_at=10.0,
                updated_at=20.0,
            )
        )
        session.add(
            EditorialLedger(
                id="hourly",
                payload={"records": [{"id": "old-used-source", "state": "cancelled"}]},
                revision=4,
            )
        )
        session.add(
            Target(
                id="max",
                profile_id="max",
                platform="max",
                route="max_browser",
                destination="synthetic",
                enabled=True,
                paused=True,
                binding_hash="a" * 64,
                options={},
            )
        )
        session.add(
            Audit(
                actor="article_reserve",
                action="reserve_preparation_claim",
                entity=claim_id,
                at=1001.0,
                details={"control_revision": 7, "contract": "article-reserve-v1"},
            )
        )
        session.add(
            Audit(
                actor="article_reserve",
                action="reserve_preparation_finished",
                entity=claim_id,
                at=1009.0,
                details={
                    "record_id": None,
                    "state": "blocked_unknown",
                    "error": recovery.OLD_ERROR,
                },
            )
        )
    case = SimpleNamespace(
        core=core,
        claim_id=claim_id,
        operation_id=str(uuid4()),
        calls=calls,
        actor="owner_maintenance:synthetic",
        objects={},
        refs={},
        paths={},
    )
    directory = Path(tmp_path) / "data" / "proofs"
    directory.mkdir(parents=True)
    case.policy_path = Path(tmp_path) / "data/hourly-article-policy.local.json"
    case.policy_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "approval_mode": "automatic",
                "presentation_contract": "article-illustrated-v2",
            }
        ),
        encoding="utf-8",
    )
    case.policy_path.chmod(0o600)

    def write_raw(name, raw):
        path = directory / (name + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        case.paths[name] = path
        case.refs[name] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        return copy.deepcopy(case.refs[name])

    def write(name, value):
        case.objects[name] = copy.deepcopy(value)
        return write_raw(name, json.dumps(value, sort_keys=True).encode())

    case.write = write
    case.write_raw = write_raw
    module_bytes = b"# synthetic public release byte assembly\n"
    module_map = {"synthetic.py": hashlib.sha256(module_bytes).hexdigest()}
    monkeypatch.setattr(recovery, "C2_PYTHON_TREE_SHA256", recovery._tree_hash(module_map))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("max_postiz_agent/synthetic.py", module_bytes)
        wheel.writestr(
            "max_postiz_agent-0.14.1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: max-postiz-agent\nVersion: 0.14.1\n",
        )
    write_raw("wheel_archive", archive.getvalue())
    process = {"pid": 1234, "start_ticks": 42, "started_at_unix": 920.0}
    source_tree, source_manifest = "a" * 40, "b" * 64
    write(
        "source_cas",
        {
            "state": "source_and_venv_switched_main_stopped",
            "source_commit": recovery.SOURCE_COMMIT,
            "source_tree": source_tree,
            "new_venv": "/synthetic/venv",
            "source_files_verified": 1,
            "finished_at": 910.0,
        },
    )
    write(
        "start",
        {
            "state": "started_health_ready_pending_full_history_verification",
            "mode": "start",
            "source_commit": recovery.SOURCE_COMMIT,
            "source_tree": source_tree,
            "main_process": process,
            "resume_performed": False,
            "Node_DB_mutations": 0,
            "finished_at": 930.0,
        },
    )
    write(
        "ready",
        {
            "state": "staged_not_installed",
            "package_version": "0.14.1",
            "source_commit": recovery.SOURCE_COMMIT,
            "source_tree": source_tree,
            "dependencies_exact": True,
            "wheel_sha256": case.refs["wheel_archive"]["sha256"],
            "venv": "/synthetic/venv",
            "source": "/synthetic/accepted-source",
            "source_files": 1,
            "source_manifest_sha256": "c" * 64,
            "source_map_sha256": source_manifest,
            "finished_at": 900.0,
        },
    )
    write(
        "installed_manifest",
        {
            "source_commit": recovery.SOURCE_COMMIT,
            "version": "0.14.1",
            "runtime_scope_files": 1,
            "files": {
                "src/max_postiz_agent/synthetic.py": {
                    "normalized_sha256": module_map["synthetic.py"]
                }
            },
            "source_tree": source_tree,
            "source_manifest_sha256": source_manifest,
            "source_cas_result_sha256": case.refs["source_cas"]["sha256"],
            "main_process": process,
            "main_pid": process["pid"],
            "created_at": 950.0,
        },
    )
    write(
        "parser_reproduction",
        {
            "contract": "root-installed-wheel-parser-readonly-repro-v1",
            "installed_version": "0.14.1",
            "module_path": "/synthetic/venv/lib/python3.12/site-packages/max_postiz_agent/server_nasa.py",
            "missing_extractor_path": "/synthetic/venv/lib/python3.12/scripts/fetch_nasa_article.py",
            "state": "installed_wheel_missing_extractor_reproduced",
            "same_missing_path": True,
            "extractor_exists": False,
            "exception_type": "FileNotFoundError",
            **dict.fromkeys(
                ("model_calls", "http_calls", "database_writes", "tick_calls", "send_calls"), 0
            ),
        },
    )
    write("selector_trace", {"synthetic": "private traceback bytes represented only by a pin"})
    outcome = {
        "exception_type": "FileNotFoundError",
        "matches_missing_wheel_extractor": True,
        "private_traceback": copy.deepcopy(case.refs["selector_trace"]),
    }
    write(
        "both_lanes_copy",
        {
            "state": "installed_wheel_both_lanes_failure_reproduced_full125_preserved",
            "full125_before_after_exact": True,
            "cleanup_exception_types": [],
            "historical_article_fixture_used_for_prepare": False,
            "selector": outcome,
            "installed_wheel_package": "/synthetic/venv/lib/python3.12/site-packages/max_postiz_agent",
            "isolation": {
                "interfaces": ["lo"],
                "external_routes": 0,
                "network_namespace": "net:[12345]",
            },
            "production_actions": 0,
            "model_requests": 0,
            "http_requests": 0,
            "real_bind_calls": 0,
            "ready": [copy.deepcopy(outcome) for _ in range(3)],
            "counts": dict.fromkeys(
                (
                    "assign_calls",
                    "bind_sentinel",
                    "forbidden_capability_calls",
                    "forbidden_http_calls",
                    "forbidden_process_calls",
                ),
                0,
            ),
        },
    )
    write(
        "diagnostic",
        {
            "contract": "unbound-preparation093-readonly-diagnostic-v1",
            "state": "evidence_collected_claim_still_blocked",
            "claim_id": claim_id,
            "claim_record_id": None,
            "claim_state": "blocked_unknown",
            "claim_error": recovery.OLD_ERROR,
            "control_revision": 7,
            "claim_reconciled": False,
            "database_writes": 0,
            "model_calls": 0,
            "http_calls": 0,
            "send_calls": 0,
            "claim_created_at": 1000.0,
            "claim_updated_at": 1009.0,
            "claim_related_record_count": 0,
            "claim_and_ledger_stable_during_capture": True,
            "source": {"manifest": {"sha256": case.refs["installed_manifest"]["sha256"]}},
            "observed_at": 1020.0,
        },
    )
    case.expected_claim_hash = recovery.inspect(core, claim_id)["claim_hash"]

    def seal(relink=True):
        with core.db.session() as session:
            control_settings = copy.deepcopy(session.get(EditorialControl, "main").settings)
        subject = {
            "source_commit": recovery.SOURCE_COMMIT,
            "wheel_sha256": case.refs["wheel_archive"]["sha256"],
            "manifest_sha256": case.refs["installed_manifest"]["sha256"],
            "package_path": "/synthetic/venv/lib/python3.12/site-packages/max_postiz_agent",
            "python_tree_sha256": recovery.C2_PYTHON_TREE_SHA256,
        }
        proof = {
            "contract": recovery.CONTRACT,
            "claim_id": claim_id,
            "claim_hash": case.expected_claim_hash,
            **{
                n: copy.deepcopy(case.refs[n])
                for n in (
                    "installed_manifest",
                    "parser_reproduction",
                    "both_lanes_copy",
                    "selector_trace",
                    "wheel_archive",
                )
            },
            "interval_attestation": {
                "actor": case.actor,
                "claim_id": claim_id,
                "claim_hash": case.expected_claim_hash,
                "control_settings_hash": fingerprint(control_settings),
                "source_settings_hash": core.settings.hash,
                "policy_sha256": hashlib.sha256(case.policy_path.read_bytes()).hexdigest(),
                **dict.fromkeys(
                    (
                        "root_authorized",
                        "layout_unchanged_from_install_through_claim",
                        "resource_never_created_or_removed",
                        "selector_none",
                        "source_configs_unchanged",
                    ),
                    True,
                ),
                "installation_evidence": {
                    n: copy.deepcopy(case.refs[n])
                    for n in ("ready", "source_cas", "start", "diagnostic")
                },
                "installation_subject": subject,
                "ordered_installation_timestamps": [900.0, 910.0, 920.0, 1020.0, 1030.0],
            },
        }
        case.proof_object = proof
        write("proof", proof)
        case.arguments = {
            "operation_id": case.operation_id,
            "expected_control_revision": 8,
            "expected_claim_hash": case.expected_claim_hash,
            "actor": case.actor,
            "proof": copy.deepcopy(case.refs["proof"]),
            "evidence_hashes": {
                n: case.refs[n]["sha256"]
                for n in (
                    "installed_manifest",
                    "parser_reproduction",
                    "both_lanes_copy",
                    "selector_trace",
                    "wheel_archive",
                    "ready",
                    "source_cas",
                    "start",
                    "diagnostic",
                )
            },
        }

    case.seal = seal
    seal()
    return case


@pytest.fixture
def unbound_case(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    value = make_case(tmp_path, engine, monkeypatch)
    yield value
    engine.dispose()


def check_inspect(case):
    before = snapshot(case)
    assert recovery.inspect(case.core, case.claim_id)["claim_hash"] == case.expected_claim_hash
    assert snapshot(case) == before and case.calls == []


def check_commit(case):
    before = snapshot(case)
    result = ArticleReserveStore(case.core).reconcile_unbound_selection_pre_request(
        case.claim_id, **case.arguments
    )
    after = snapshot(case)
    assert result["state"] == "aborted_selection_pre_request" and case.calls == []
    assert {t for t in before if before[t] != after[t]} == {
        "editorial_preparation_claims",
        "audit_events",
    }
    old = next(r for r in before["editorial_preparation_claims"] if r["id"] == case.claim_id)
    new = next(r for r in after["editorial_preparation_claims"] if r["id"] == case.claim_id)
    assert {k for k in old if old[k] != new[k]} == {"state", "active_key", "updated_at"}
    assert new["state"] == "diagnostic_no_upstream" and new["error"] == old["error"]
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    return result


def check_replay(case):
    first = check_commit(case)
    before = snapshot(case)
    case.paths["proof"].unlink()  # replay/status must not resample any proof or upstream
    assert recovery.apply(case.core, case.claim_id, **case.arguments) == first
    assert recovery.operation_status(case.core, case.operation_id) == first
    assert snapshot(case) == before


def check_reject(case, change):
    change(case)
    before = snapshot(case)
    with pytest.raises(AgentError):
        recovery.apply(case.core, case.claim_id, **case.arguments)
    assert snapshot(case) == before and case.calls == []


def mutate_row(case, model, identity, **changes):
    with case.core.db.session.begin() as session:
        row = session.get(model, identity)
        for k, value in changes.items():
            setattr(row, k, value)


def change_proof(case, name, mutate):
    value = copy.deepcopy(case.objects[name])
    mutate(value)
    case.write(name, value)
    case.seal(relink=name not in {"ready", "source_cas", "start", "diagnostic"})


def test_inspect_readonly(unbound_case):
    check_inspect(unbound_case)


def test_atomic_history_preserving_disposition(unbound_case):
    check_commit(unbound_case)


def test_response_loss_replay_and_status_no_more_reads(unbound_case):
    check_replay(unbound_case)


@pytest.mark.parametrize(
    "damage", ["running", "revision", "claim", "bound", "target", "artifact", "seal", "actor"]
)
def test_unsafe_context_refused(unbound_case, damage):
    c = unbound_case

    def change(value):
        if damage == "running":
            mutate_row(value, EditorialControl, "main", paused=False)
        if damage == "revision":
            value.arguments["expected_control_revision"] = 9
        if damage == "claim":
            value.arguments["expected_claim_hash"] = "0" * 64
        if damage == "bound":
            mutate_row(value, EditorialPreparationClaim, value.claim_id, record_id=str(uuid4()))
        if damage == "target":
            mutate_row(value, Target, "max", paused=False)
        if damage == "artifact":
            path = value.core.settings.root / "data/hourly-articles"
            path.mkdir()
            (path / "quality-orphan.jsonl").write_text("unknown")
        if damage == "seal":
            value.arguments["evidence_hashes"]["both_lanes_copy"] = "0" * 64
        if damage == "actor":
            value.arguments["actor"] = "unprivileged"

    check_reject(c, change)


@pytest.mark.parametrize(
    "name,field,bad",
    [
        ("parser_reproduction", "extractor_exists", True),
        ("parser_reproduction", "http_calls", 1),
        ("both_lanes_copy", "full125_before_after_exact", False),
        ("both_lanes_copy", "historical_article_fixture_used_for_prepare", True),
        ("installed_manifest", "source_commit", "foreign"),
    ],
)
def test_sealed_invalid_semantic_proof_rejected(unbound_case, name, field, bad):
    check_reject(unbound_case, lambda c: change_proof(c, name, lambda p: p.update({field: bad})))


def test_historical_interval_is_required(unbound_case):
    c = unbound_case
    c.proof_object["interval_attestation"]["resource_never_created_or_removed"] = False
    c.write("proof", c.proof_object)
    c.arguments["proof"] = copy.deepcopy(c.refs["proof"])
    check_reject(c, lambda _: None)


def test_exact_native_lock_is_not_upstream_artifact(unbound_case):
    c = unbound_case
    directory = c.core.settings.root / "data/hourly-articles"
    directory.mkdir()
    (directory / "cycle.lock").write_bytes(b"0")
    check_commit(c)


@pytest.mark.parametrize("damage", ["action", "actor", "row"])
def test_status_drift_never_claims_completed(unbound_case, damage):
    c = unbound_case
    result = check_commit(c)
    if damage == "row":
        mutate_row(c, EditorialPreparationClaim, c.claim_id, error="changed")
    else:
        mutate_row(c, Audit, result["audit_id"], **{damage: "changed"})
    with pytest.raises(AgentError):
        recovery.operation_status(c.core, c.operation_id)


def test_unrelated_active_lease_blocks(unbound_case):
    c = unbound_case
    with c.core.db.session.begin() as session:
        session.add(
            RecurringTask(
                id="unrelated",
                profile_id="max",
                interval_seconds=3600,
                lookback_hours=24,
                due_at=0,
                owner="owner",
                lease_until=0,
            )
        )
    check_reject(c, lambda _: None)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("installed_wheel_package", "/foreign/site-packages/max_postiz_agent"),
        ("production_actions", 1),
        ("model_requests", 1),
        (
            "isolation",
            {
                "interfaces": ["lo", "eth0"],
                "external_routes": 1,
                "network_namespace": "net:[12345]",
            },
        ),
    ],
)
def test_copy_subject_and_capabilities_are_semantic_guards(unbound_case, field, bad):
    check_reject(
        unbound_case, lambda c: change_proof(c, "both_lanes_copy", lambda p: p.update({field: bad}))
    )


def test_nested_lock_name_is_an_unknown_new_artifact(unbound_case):
    c = unbound_case
    directory = c.core.settings.root / "data/hourly-articles/unrelated"
    directory.mkdir(parents=True)
    (directory / "cycle.lock").write_bytes(b"0")
    check_reject(c, lambda _: None)


def test_owner_sealed_wrong_installation_subject_is_rejected(unbound_case):
    def wrong(p):
        p["venv"] = "/foreign/venv"

    check_reject(unbound_case, lambda c: change_proof(c, "ready", wrong))


def test_wrong_wheel_byte_assembly_is_rejected_even_if_owner_resealed(unbound_case):
    c = unbound_case
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("max_postiz_agent/synthetic.py", b"different release code")
        wheel.writestr(
            "max_postiz_agent-0.14.1.dist-info/METADATA",
            "Name: max-postiz-agent\nVersion: 0.14.1\n",
        )
    c.write_raw("wheel_archive", archive.getvalue())
    c.seal()
    check_reject(c, lambda _: None)


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_wheel_metadata_lf_and_crlf_are_supported(unbound_case, ending):
    c = unbound_case
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(
            "max_postiz_agent/synthetic.py", b"# synthetic public release byte assembly\n"
        )
        wheel.writestr(
            "max_postiz_agent-0.14.1.dist-info/METADATA",
            ending.join(("Metadata-Version: 2.1", "Name: max-postiz-agent", "Version: 0.14.1", "")),
        )
    c.write_raw("wheel_archive", archive.getvalue())
    c.write("ready", {**c.objects["ready"], "wheel_sha256": c.refs["wheel_archive"]["sha256"]})
    c.seal()
    check_commit(c)


@pytest.mark.parametrize("damage", ["duplicate_version", "wrong_version", "startup_pth"])
def test_wrong_wheel_metadata_and_startup_assembly_refused(unbound_case, damage):
    c = unbound_case
    metadata = "Metadata-Version: 2.1\nName: max-postiz-agent\nVersion: 0.14.1\n"
    if damage == "duplicate_version":
        metadata += "Version: 0.14.1\n"
    if damage == "wrong_version":
        metadata = metadata.replace("0.14.1", "99.0")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(
            "max_postiz_agent/synthetic.py", b"# synthetic public release byte assembly\n"
        )
        wheel.writestr("max_postiz_agent-0.14.1.dist-info/METADATA", metadata)
        if damage == "startup_pth":
            wheel.writestr("startup.pth", "import external")
    c.write_raw("wheel_archive", archive.getvalue())
    c.write("ready", {**c.objects["ready"], "wheel_sha256": c.refs["wheel_archive"]["sha256"]})
    c.seal()
    check_reject(c, lambda _: None)


def check_audit_failure_rollback(case):
    before = snapshot(case)

    def broken(_mapper, _connection, audit):
        if audit.action == recovery.ACTION:
            raise RuntimeError("synthetic failure after claim UPDATE before commit")

    event.listen(Audit, "before_insert", broken)
    try:
        with pytest.raises(RuntimeError):
            recovery.apply(case.core, case.claim_id, **case.arguments)
    finally:
        event.remove(Audit, "before_insert", broken)
    assert snapshot(case) == before
    check_commit(case)


def test_claim_update_rolls_back_if_audit_write_fails(unbound_case):
    check_audit_failure_rollback(unbound_case)


def test_related_ledger_record_refuses_no_upstream_disposition(unbound_case):
    c = unbound_case
    with c.core.db.session.begin() as session:
        ledger = session.get(EditorialLedger, "hourly")
        ledger.payload = {"records": [{"id": str(uuid4()), "reserve_claim": {"id": c.claim_id}}]}
    check_reject(c, lambda _: None)


def change_policy(case, **values):
    policy = json.loads(case.policy_path.read_bytes())
    policy.update(values)
    case.policy_path.write_text(json.dumps(policy), encoding="utf-8")
    case.seal()


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": False},
        {"approval_mode": "human"},
        {"presentation_contract": None},
        {"presentation_contract": "unillustrated"},
    ],
)
def test_only_supported_illustrated_automatic_policy_even_when_resealed(unbound_case, values):
    check_reject(unbound_case, lambda c: change_policy(c, **values))


def test_control_source_resealed_as_other_mode_is_refused(unbound_case):
    def change(c):
        mutate_row(c, EditorialControl, "main", settings={"source": "nasa_svs"})
        c.seal()

    check_reject(unbound_case, change)


def test_policy_current_bytes_must_match_owner_seal(unbound_case):
    c = unbound_case
    c.policy_path.write_text(c.policy_path.read_text() + "\n", encoding="utf-8")
    check_reject(c, lambda _: None)


def test_settings_hash_must_match_owner_historical_seal(unbound_case):
    c = unbound_case
    c.proof_object["interval_attestation"]["source_settings_hash"] = "0" * 64
    c.write("proof", c.proof_object)
    c.arguments["proof"] = copy.deepcopy(c.refs["proof"])
    check_reject(c, lambda _: None)


def test_readiness_raw_manifest_and_installed_canonical_map_have_distinct_ids(unbound_case):
    c = unbound_case
    ready, installed = c.objects["ready"], c.objects["installed_manifest"]
    assert ready["source_manifest_sha256"] != installed["source_manifest_sha256"]
    assert ready["source_map_sha256"] == installed["source_manifest_sha256"]
    check_commit(c)
