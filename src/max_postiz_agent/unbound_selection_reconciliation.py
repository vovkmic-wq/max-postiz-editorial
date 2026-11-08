"""Owner-only disposition of the proved C2 installed-extractor pre-request failure.

No selector, model, transport, initialization or implicit recovery is invoked.
The candidate domain can run in an explicitly authorized maintenance sidecar.
"""

import copy
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import zipfile
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select

from .article_attribution import illustrated_presentation
from .config import AgentError, fingerprint
from .db import Audit, Intent, Job, now
from .hub_models import BridgePost, Dispatch, RecurringTask, RemoteDraft, Target, Variant
from .server_models import (
    EditorialControl,
    EditorialLedger,
    EditorialPreparationClaim,
    PublicationSlot,
)
from .server_state import CONTROL_ID, fence_editorial_configuration

CONTRACT = "aborted-selection-pre-request-v1"
ACTION = "reserve_unbound_selection_aborted_pre_request"
OLD_ERROR = "ARTICLE_RESERVE_PREPARATION_INTERRUPTED"
SOURCE_COMMIT = "c2f1fcc2619aa01e604c5316636e006b96f26dc1"
# Digest of the complete public C2 package Python map (110 files, Git LF bytes).
C2_PYTHON_TREE_SHA256 = "f2f63e3b2755db3a0d6d57ab3bcb580a963b5994fb5fc263c77dead6fdbb8181"
MINIMUM_RUNTIME_VERSION = "0.14.2"
_TERMINAL = {
    "quality_ready",
    "rejected",
    "no_source",
    "abandoned_unknown",
    "diagnostic_complete",
    "diagnostic_no_upstream",
    "failed_input_planning",
    "failed_pre_model_validation",
    "failed_provider_rejection",
}


def require(value, code="UNBOUND_SELECTION_NOT_SAFE"):
    if not value:
        raise AgentError(code, 409)


def _view(row):
    return {c.key: copy.deepcopy(getattr(row, c.key)) for c in row.__table__.columns}


def _operation(value):
    try:
        require(
            isinstance(value, str) and str(UUID(value)) == value, "UNBOUND_OPERATION_ID_REQUIRED"
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError("UNBOUND_OPERATION_ID_REQUIRED", 409) from exc
    return str(uuid5(NAMESPACE_URL, CONTRACT + ":" + value))


def _pin(core, ref, trusted_proof_root=None, limit=2 * 1024 * 1024, readable_config=False):
    require(
        isinstance(ref, dict)
        and set(ref) == {"path", "sha256"}
        and isinstance(ref.get("path"), str)
        and isinstance(ref.get("sha256"), str)
        and re.fullmatch("[a-f0-9]{64}", ref.get("sha256", ""))
    )
    path = Path(ref["path"])
    roots = [core.settings.root / "data"]
    if trusted_proof_root is not None:
        extra = Path(trusted_proof_root)
        require(
            os.name == "posix"
            and os.geteuid() == 0
            and extra.is_absolute()
            and not any(p.is_symlink() for p in (extra, *extra.parents))
            and extra.is_dir()
            and extra.stat().st_uid == 0
            and extra.stat().st_mode & 0o077 == 0
        )
        roots.append(extra)
    require(
        path.is_absolute()
        and ".." not in path.parts
        and any(path.is_relative_to(p) for p in roots)
        and not any(p.is_symlink() for p in (path, *path.parents))
    )
    try:
        before = path.stat()
        require(path.is_file() and before.st_nlink == 1 and 0 < before.st_size <= limit)
        if os.name == "posix":
            require(before.st_mode & (0o022 if readable_config else 0o077) == 0)
        raw = path.read_bytes()
        after = path.stat()
        fields = (
            "st_ino",
            "st_dev",
            "st_size",
            "st_mtime_ns",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
        )
        require(
            all(getattr(before, k) == getattr(after, k) for k in fields)
            and hashlib.sha256(raw).hexdigest() == ref["sha256"]
        )
        return raw
    except (OSError, ValueError, TypeError) as exc:
        raise AgentError("UNBOUND_PROOF_UNAVAILABLE", 409) from exc


def _json(core, ref, trusted_proof_root=None):
    try:
        value = json.loads(_pin(core, ref, trusted_proof_root))
        require(isinstance(value, dict))
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AgentError("UNBOUND_PROOF_INVALID", 409) from exc


def _tree_hash(mapping):
    return hashlib.sha256(
        json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _wheel(core, ref, manifest, trusted_proof_root):
    raw = _pin(core, ref, trusted_proof_root, limit=16 * 1024 * 1024)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            require(
                len(infos) <= 3000
                and len({i.filename for i in infos}) == len(infos)
                and sum(i.file_size for i in infos) <= 32 * 1024 * 1024
            )
            names = {i.filename for i in infos}
            package_names = {
                n.removeprefix("src/")
                for n in manifest["files"]
                if n.startswith("src/max_postiz_agent/")
            }
            metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
            require(len(metadata) == 1)
            info = BytesParser(policy=email_policy).parsebytes(archive.read(metadata[0]))
            require(
                not info.defects
                and len(info.get_all("Version", [])) == 1
                and len(info.get_all("Name", [])) == 1
                and str(info["Version"]) == "0.14.1"
                and str(info["Name"]).replace("_", "-") == "max-postiz-agent"
            )
            dist_info = metadata[0].split("/")[0]
            allowed_metadata = {
                dist_info + "/" + n
                for n in ("METADATA", "WHEEL", "RECORD", "top_level.txt", "entry_points.txt")
            }
            require(
                all(n in package_names or n in allowed_metadata or n.endswith("/") for n in names)
                and {n for n in names if n.startswith("max_postiz_agent/") and not n.endswith("/")}
                == package_names
            )
            require(
                all(
                    hashlib.sha256(archive.read(name).replace(b"\r\n", b"\n")).hexdigest()
                    == manifest["files"]["src/" + name]["normalized_sha256"]
                    for name in package_names
                )
            )
            mapping = {
                n.removeprefix("max_postiz_agent/"): hashlib.sha256(
                    archive.read(n).replace(b"\r\n", b"\n")
                ).hexdigest()
                for n in names
                if n.startswith("max_postiz_agent/") and n.endswith(".py")
            }
        expected = {
            n.removeprefix("src/max_postiz_agent/"): v["normalized_sha256"]
            for n, v in manifest["files"].items()
            if n.startswith("src/max_postiz_agent/") and n.endswith(".py")
        }
        require(mapping == expected and _tree_hash(mapping) == C2_PYTHON_TREE_SHA256)
        return ref["sha256"]
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise AgentError("UNBOUND_WHEEL_ASSEMBLY_INVALID", 409) from exc


def _proof(core, ref, claim, control, actor, evidence_hashes, trusted_proof_root):
    value = _json(core, ref, trusted_proof_root)
    require(
        set(value)
        == {
            "contract",
            "claim_id",
            "claim_hash",
            "installed_manifest",
            "parser_reproduction",
            "both_lanes_copy",
            "selector_trace",
            "wheel_archive",
            "interval_attestation",
        }
        and value["contract"] == CONTRACT
        and value["claim_id"] == claim.id
        and value["claim_hash"] == fingerprint(_view(claim))
    )
    names = {
        "installed_manifest",
        "parser_reproduction",
        "both_lanes_copy",
        "selector_trace",
        "wheel_archive",
        "ready",
        "source_cas",
        "start",
        "diagnostic",
    }
    require(
        isinstance(evidence_hashes, dict)
        and set(evidence_hashes) == names
        and all(
            isinstance(v, str) and re.fullmatch("[a-f0-9]{64}", v) for v in evidence_hashes.values()
        )
    )
    for name in names - {"ready", "source_cas", "start", "diagnostic"}:
        require(value[name].get("sha256") == evidence_hashes[name])
    manifest = _json(core, value["installed_manifest"], trusted_proof_root)
    parser = _json(core, value["parser_reproduction"], trusted_proof_root)
    reproduced = _json(core, value["both_lanes_copy"], trusted_proof_root)
    _pin(core, value["selector_trace"], trusted_proof_root)
    require(
        manifest["source_commit"] == SOURCE_COMMIT
        and manifest["version"] == "0.14.1"
        and type(manifest["runtime_scope_files"]) is int
        and manifest["runtime_scope_files"] > 0
    )
    wheel_hash = _wheel(core, value["wheel_archive"], manifest, trusted_proof_root)
    module = PurePosixPath(parser.get("module_path", ""))
    missing = PurePosixPath(parser.get("missing_extractor_path", ""))
    require(
        module.is_absolute()
        and module.name == "server_nasa.py"
        and module.parent.name == "max_postiz_agent"
        and module.parent.parent.name == "site-packages"
        and missing == module.parents[2] / "scripts/fetch_nasa_article.py"
        and parser.get("contract") == "root-installed-wheel-parser-readonly-repro-v1"
        and parser.get("installed_version") == "0.14.1"
        and parser.get("state") == "installed_wheel_missing_extractor_reproduced"
        and parser.get("same_missing_path") is True
        and parser.get("extractor_exists") is False
        and parser.get("exception_type") == "FileNotFoundError"
        and all(
            parser.get(k) == 0
            for k in ("model_calls", "http_calls", "database_writes", "tick_calls", "send_calls")
        )
    )
    outcomes = [reproduced.get("selector", {}), *reproduced.get("ready", [])]
    require(
        reproduced.get("state") == "installed_wheel_both_lanes_failure_reproduced_full125_preserved"
        and reproduced.get("installed_wheel_package") == str(module.parent)
        and reproduced.get("isolation", {}).get("interfaces") == ["lo"]
        and reproduced.get("isolation", {}).get("external_routes") == 0
        and re.fullmatch(
            r"net:\[[0-9]+\]", reproduced.get("isolation", {}).get("network_namespace", "")
        )
        and all(
            reproduced.get(k) == 0
            for k in ("production_actions", "model_requests", "http_requests", "real_bind_calls")
        )
        and reproduced.get("full125_before_after_exact") is True
        and reproduced.get("cleanup_exception_types") == []
        and reproduced.get("historical_article_fixture_used_for_prepare") is False
        and len(outcomes) == 4
        and all(
            x.get("exception_type") == "FileNotFoundError"
            and x.get("matches_missing_wheel_extractor") is True
            for x in outcomes
        )
        and reproduced.get("selector", {}).get("private_traceback", {}).get("sha256")
        == evidence_hashes["selector_trace"]
        and reproduced.get("counts")
        == {
            "assign_calls": 0,
            "bind_sentinel": 0,
            "forbidden_capability_calls": 0,
            "forbidden_http_calls": 0,
            "forbidden_process_calls": 0,
        }
    )
    att = value["interval_attestation"]
    require(
        isinstance(att, dict)
        and att.get("actor") == actor
        and att.get("claim_id") == claim.id
        and att.get("claim_hash") == value["claim_hash"]
        and att.get("control_settings_hash") == fingerprint(control.settings)
        and all(
            att.get(k) is True
            for k in (
                "root_authorized",
                "layout_unchanged_from_install_through_claim",
                "resource_never_created_or_removed",
                "selector_none",
                "source_configs_unchanged",
            )
        )
    )
    require(
        control.settings.get("source") == "open_science"
        and isinstance(core.settings.hash, str)
        and re.fullmatch("[a-f0-9]{64}", core.settings.hash)
        and att.get("source_settings_hash") == core.settings.hash
    )
    policy_sha = att.get("policy_sha256")
    policy_raw = _pin(
        core,
        {
            "path": str(core.settings.root / "data/hourly-article-policy.local.json"),
            "sha256": policy_sha,
        },
        limit=64 * 1024,
        readable_config=True,
    )
    try:
        policy = json.loads(policy_raw)
    except (ValueError, UnicodeError) as exc:
        raise AgentError("UNBOUND_POLICY_INVALID", 409) from exc
    # This recovery contract intentionally supports only the illustrated
    # automatic open-science configuration proved by the owner, not other modes.
    require(
        isinstance(policy, dict)
        and policy.get("enabled") is True
        and policy.get("approval_mode") == "automatic"
        and illustrated_presentation(policy.get("presentation_contract"))
    )
    refs = att.get("installation_evidence", {})
    require(set(refs) == {"ready", "source_cas", "start", "diagnostic"})
    subject = {
        "source_commit": SOURCE_COMMIT,
        "wheel_sha256": wheel_hash,
        "manifest_sha256": evidence_hashes["installed_manifest"],
        "package_path": str(module.parent),
        "python_tree_sha256": C2_PYTHON_TREE_SHA256,
    }
    require(att.get("installation_subject") == subject)
    for name, evidence in refs.items():
        require(evidence.get("sha256") == evidence_hashes[name])
    ready, cas, started, diagnostic = (
        _json(core, refs[name], trusted_proof_root)
        for name in ("ready", "source_cas", "start", "diagnostic")
    )
    venv = module.parents[4]
    process = manifest.get("main_process", {})
    count = len(manifest["files"])
    require(
        ready.get("state") == "staged_not_installed"
        and ready.get("package_version") == "0.14.1"
        and ready.get("source_commit") == SOURCE_COMMIT
        and ready.get("source_tree") == manifest.get("source_tree")
        and ready.get("dependencies_exact") is True
        and ready.get("wheel_sha256") == wheel_hash
        and ready.get("venv") == str(venv)
        and ready.get("source") == str(venv.parent / "accepted-source")
        and ready.get("source_files") == count
        and ready.get("source_map_sha256") == manifest.get("source_manifest_sha256")
    )
    require(
        cas.get("state") == "source_and_venv_switched_main_stopped"
        and cas.get("source_commit") == SOURCE_COMMIT
        and cas.get("source_tree") == manifest.get("source_tree")
        and cas.get("new_venv") == str(venv)
        and cas.get("source_files_verified") == count
        and manifest.get("source_cas_result_sha256") == refs["source_cas"]["sha256"]
    )
    require(
        started.get("state") == "started_health_ready_pending_full_history_verification"
        and started.get("mode") == "start"
        and started.get("source_commit") == SOURCE_COMMIT
        and started.get("source_tree") == manifest.get("source_tree")
        and started.get("main_process") == process
        and process.get("pid") == manifest.get("main_pid")
        and type(process.get("pid")) is int
        and process["pid"] > 0
        and type(process.get("start_ticks")) is int
        and process["start_ticks"] > 0
        and started.get("resume_performed") is False
        and started.get("Node_DB_mutations") == 0
    )
    require(
        diagnostic.get("contract") == "unbound-preparation093-readonly-diagnostic-v1"
        and diagnostic.get("state") == "evidence_collected_claim_still_blocked"
        and diagnostic.get("claim_id") == claim.id
        and diagnostic.get("claim_record_id") is None
        and diagnostic.get("claim_state") == "blocked_unknown"
        and diagnostic.get("claim_error") == OLD_ERROR
        and diagnostic.get("control_revision") == claim.control_revision
        and diagnostic.get("claim_reconciled") is False
        and diagnostic.get("claim_created_at") == claim.created_at
        and diagnostic.get("claim_updated_at") == claim.updated_at
        and diagnostic.get("claim_related_record_count") == 0
        and diagnostic.get("claim_and_ledger_stable_during_capture") is True
        and all(
            diagnostic.get(k) == 0
            for k in ("database_writes", "model_calls", "http_calls", "send_calls")
        )
        and diagnostic.get("source", {}).get("manifest", {}).get("sha256")
        == evidence_hashes["installed_manifest"]
    )
    stamps = [
        ready.get("finished_at"),
        cas.get("finished_at"),
        process.get("started_at_unix"),
        started.get("finished_at"),
        manifest.get("created_at"),
        claim.created_at,
        claim.updated_at,
        diagnostic.get("observed_at"),
        now(),
    ]
    require(
        all(type(v) in {int, float} and math.isfinite(v) for v in stamps)
        and 0 <= stamps[0]
        and stamps == sorted(stamps)
    )

    return {
        "proof": copy.deepcopy(ref),
        "evidence_hashes": copy.deepcopy(evidence_hashes),
        "source_commit": SOURCE_COMMIT,
        "policy_sha256": policy_sha,
        "source_settings_hash": core.settings.hash,
        "installation_evidence": refs,
        "no_selected_source": True,
        "source_url_ignored_as_stale": True,
    }


def _artifacts(core, claim):
    for relative in ("data/hourly-articles", "data/opencode-requests"):
        directory = core.settings.root / relative
        if not directory.exists():
            continue
        require(
            not any(p.is_symlink() for p in (directory, *directory.parents)) and directory.is_dir()
        )
        entries = list(directory.iterdir())
        require(len(entries) <= 5000)
        for path in entries:
            require(not path.is_symlink())
            children = list(path.iterdir()) if path.is_dir() else [path]
            require(len(children) <= 64)
            for child in children:
                require(not child.is_symlink() and child.is_file())
                if (
                    relative == "data/hourly-articles"
                    and child.parent == directory
                    and child.name in {"cycle.lock", "reserve-preparation.lock"}
                ):
                    require(child.stat().st_size == 1 and child.read_bytes() == b"0")
                    continue
                require(claim.id not in child.name and child.stat().st_mtime < claim.created_at - 1)


def _quiet(session, claim):
    from .preapproval_retirement import assert_no_unfinished

    assert_no_unfinished(session)
    require(not session.scalar(select(Target.id).where(Target.paused.is_not(True))))
    require(
        not session.scalar(
            select(EditorialPreparationClaim.id).where(
                EditorialPreparationClaim.id != claim.id,
                or_(
                    EditorialPreparationClaim.active_key.is_not(None),
                    EditorialPreparationClaim.state.is_(None),
                    EditorialPreparationClaim.state.not_in(_TERMINAL),
                ),
            )
        )
    )
    for model, condition in (
        (
            Dispatch,
            or_(
                Dispatch.state.is_(None),
                Dispatch.state.not_in({"published", "cancelled"}),
                Dispatch.owner.is_not(None),
                Dispatch.lease_until.is_(None),
                Dispatch.lease_until != 0,
            ),
        ),
        (
            BridgePost,
            or_(
                BridgePost.mutation_owner.is_not(None),
                BridgePost.mutation_until.is_(None),
                BridgePost.mutation_until != 0,
            ),
        ),
        (
            Job,
            or_(
                Job.state.is_(None),
                Job.state.not_in({"done", "failed", "cancelled"}),
                Job.owner.is_not(None),
                Job.lease_until.is_(None),
                Job.lease_until != 0,
            ),
        ),
        (
            RecurringTask,
            or_(
                RecurringTask.owner.is_not(None),
                RecurringTask.lease_until.is_(None),
                RecurringTask.lease_until != 0,
            ),
        ),
        (
            Intent,
            or_(
                Intent.state.is_(None),
                Intent.state.not_in({"confirmed", "rejected", "archived", "cancelled"}),
            ),
        ),
        (RemoteDraft, or_(RemoteDraft.state.is_(None), RemoteDraft.state != "draft")),
        (PublicationSlot, PublicationSlot.active_target.is_not(None)),
    ):
        require(session.scalar(select(model).where(condition).limit(1)) is None)


def _unbound(session, claim):
    _operation(claim.token)
    require(
        type(claim.control_revision) is int
        and claim.control_revision > 0
        and claim.record_id is None
        and claim.active_key == "models"
        and claim.state == "blocked_unknown"
        and claim.error == OLD_ERROR
        and all(
            type(v) in {int, float} and math.isfinite(v)
            for v in (claim.created_at, claim.updated_at)
        )
        and claim.updated_at >= claim.created_at
        and claim.created_at >= 0
    )
    rows = list(session.scalars(select(Audit).where(Audit.entity == claim.id)))
    require(len(rows) == 2)
    begin = [a for a in rows if a.action == "reserve_preparation_claim"]
    end = [a for a in rows if a.action == "reserve_preparation_finished"]
    require(
        len(begin) == len(end) == 1
        and begin[0].actor == end[0].actor == "article_reserve"
        and begin[0].details
        == {"control_revision": claim.control_revision, "contract": "article-reserve-v1"}
        and end[0].details == {"record_id": None, "state": "blocked_unknown", "error": OLD_ERROR}
        and claim.created_at - 1 <= begin[0].at <= end[0].at <= claim.updated_at + 1
    )
    for ledger in session.scalars(select(EditorialLedger).with_for_update()):
        require(
            not any(
                isinstance(r.get("reserve_claim"), dict)
                and (
                    r["reserve_claim"].get("id") == claim.id
                    or r["reserve_claim"].get("token") == claim.token
                )
                for r in ledger.payload.get("records", [])
            )
        )
    for variant in session.scalars(select(Variant)):
        binding = variant.metadata_json.get("reserve_claim")
        require(binding is None or isinstance(binding, dict))
        require(not binding or binding.get("id") != claim.id)


def inspect(core, claim_id):
    _operation(claim_id)
    with core.db.session() as session:
        row = session.get(EditorialPreparationClaim, claim_id)
        control = session.get(EditorialControl, CONTROL_ID)
        require(row is not None and control is not None)
        return {
            "contract": CONTRACT,
            "claim_id": row.id,
            "claim_hash": fingerprint(_view(row)),
            "state": row.state,
            "error": row.error,
            "claim_control_revision": row.control_revision,
            "paused": control.paused,
            "control_revision": control.revision,
            "mutations": 0,
        }


def _committed(session, audit):
    details = audit.details
    require(
        audit.action == ACTION
        and isinstance(audit.entity, str)
        and isinstance(audit.actor, str)
        and re.fullmatch(r"owner_maintenance:[A-Za-z0-9_.-]{1,45}", audit.actor)
        and isinstance(details, dict)
        and details.get("disposition") == CONTRACT
        and details.get("minimum_runtime_version") == MINIMUM_RUNTIME_VERSION
        and audit.id == _operation(details.get("operation_id")),
        "UNBOUND_COMMITTED_STATE_CHANGED",
    )
    before, after = details.get("before"), details.get("after")
    require(
        isinstance(before, dict)
        and isinstance(after, dict)
        and set(before) == set(after)
        and fingerprint(before) == details.get("before_hash")
        and fingerprint(after) == details.get("after_hash")
        and before.get("id") == audit.entity
        and before.get("state") == "blocked_unknown"
        and before.get("active_key") == "models"
        and before.get("error") == OLD_ERROR
        and type(before.get("control_revision")) is int
        and before["control_revision"] > 0
        and before.get("record_id") is None
        and after.get("state") == "diagnostic_no_upstream"
        and after.get("active_key") is None
        and after.get("updated_at", 0) >= before.get("updated_at", 0)
        and all(
            before[k] == after[k] for k in before if k not in {"state", "active_key", "updated_at"}
        ),
        "UNBOUND_COMMITTED_STATE_CHANGED",
    )
    row = session.get(EditorialPreparationClaim, audit.entity)
    require(
        row is not None and fingerprint(_view(row)) == details["after_hash"],
        "UNBOUND_COMMITTED_STATE_CHANGED",
    )
    return {
        "contract": CONTRACT,
        "state": "aborted_selection_pre_request",
        "operation_id": details["operation_id"],
        "claim_id": audit.entity,
        "audit_id": audit.id,
        "after_hash": details["after_hash"],
        "minimum_runtime_version": MINIMUM_RUNTIME_VERSION,
        "model_calls": 0,
        "http_calls": 0,
    }


def operation_status(core, operation_id):
    audit_id = _operation(operation_id)
    with core.db.session() as session:
        audit = session.get(Audit, audit_id)
        return (
            _committed(session, audit)
            if audit
            else {"contract": CONTRACT, "state": "not_committed", "operation_id": operation_id}
        )


def apply(
    core,
    claim_id,
    *,
    operation_id,
    expected_control_revision,
    expected_claim_hash,
    proof,
    actor,
    evidence_hashes,
    trusted_proof_root=None,
):
    audit_id = _operation(operation_id)
    _operation(claim_id)
    require(
        isinstance(actor, str) and re.fullmatch(r"owner_maintenance:[A-Za-z0-9_.-]{1,45}", actor)
    )
    with core.db.session.begin() as session:
        fence_editorial_configuration(session)
        control = session.scalar(
            select(EditorialControl).where(EditorialControl.id == CONTROL_ID).with_for_update()
        )
        claim = session.scalar(
            select(EditorialPreparationClaim)
            .where(EditorialPreparationClaim.id == claim_id)
            .with_for_update()
        )
        require(control is not None and claim is not None)
        existing = session.get(Audit, audit_id)
        if existing:
            require(
                existing.action == ACTION
                and existing.entity == claim_id
                and existing.actor == actor
                and existing.details["before_hash"] == expected_claim_hash
                and existing.details["proof"] == proof
                and existing.details["evidence_hashes"] == evidence_hashes
                and existing.details["paused_revision"] == expected_control_revision
            )
            return _committed(session, existing)
        require(
            type(expected_control_revision) is int
            and control.paused is True
            and control.revision == expected_control_revision == claim.control_revision + 1
            and fingerprint(_view(claim)) == expected_claim_hash
        )
        _unbound(session, claim)
        _quiet(session, claim)
        _artifacts(core, claim)
        verified = _proof(core, proof, claim, control, actor, evidence_hashes, trusted_proof_root)
        before = _view(claim)
        stamp = now()
        require(type(stamp) in {int, float} and math.isfinite(stamp) and stamp >= claim.updated_at)
        claim.state, claim.active_key, claim.updated_at = "diagnostic_no_upstream", None, stamp
        session.flush()
        details = {
            **verified,
            "operation_id": operation_id,
            "before_hash": fingerprint(before),
            "after_hash": fingerprint(_view(claim)),
            "before": before,
            "after": _view(claim),
            "paused_revision": control.revision,
            "disposition": CONTRACT,
            "original_error": OLD_ERROR,
            "minimum_runtime_version": MINIMUM_RUNTIME_VERSION,
            "model_calls": 0,
            "http_calls": 0,
            "send_calls": 0,
            "source_id": None,
        }
        audit = Audit(id=audit_id, actor=actor, action=ACTION, entity=claim_id, details=details)
        session.add(audit)
        session.flush()
        return _committed(session, audit)
