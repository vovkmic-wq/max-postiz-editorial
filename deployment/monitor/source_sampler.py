"""Private bounded RO sampler. Receives only non-secret context; credentials stay VM.

No SQL/application imports/initialize/control/POST/browser/model. STDOUT is only
sanitized monitor-snapshot-v1 plus truthful SSH/service/manifest binding.
"""

import ast
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import types
import unicodedata
from urllib.parse import urlsplit

ROOT = Path("/opt/max-postiz")
BINDING = "pinned-editorial-monitor-v1"
SERVICE = "max-postiz-editorial.service"
PURE = {
    "_stamp",
    "_number",
    "_choice",
    "_error",
    "_text",
    "_ident",
    "_link",
    "_destination",
    "_editorial",
    "_routes",
}
DEADLINE = None


class Refused(RuntimeError):
    pass


def need(v, code):
    if not v:
        raise Refused(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def tick():
    need(DEADLINE is None or time.monotonic() < DEADLINE, "SAMPLER_DEADLINE")


def plain(path):
    path = Path(path)
    need(
        path.is_absolute()
        and ".." not in path.parts
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "SOURCE_PATH_INVALID",
    )
    return path


def read(path, limit=4 * 1024**2):
    tick()
    path = plain(path)
    before = path.stat()
    need(path.is_file() and before.st_size <= limit, "SOURCE_FILE_BOUND")
    value = path.read_bytes()
    after = path.stat()
    need(
        (before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_ino, after.st_size, after.st_mtime_ns)
        and len(value) <= limit,
        "SOURCE_FILE_CHANGED",
    )
    return value


def unit():
    tick()
    p = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            SERVICE,
            "--property=Id,ActiveState,MainPID,User,WorkingDirectory",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )
    need(p.returncode == 0, "SERVICE_UNAVAILABLE")
    v = dict(x.split("=", 1) for x in p.stdout.decode().splitlines() if "=" in x)
    need(
        v.get("Id") == SERVICE
        and v.get("User") == "editorial"
        and v.get("WorkingDirectory") == str(ROOT),
        "FOREIGN_SERVICE",
    )
    return v


def process(pid):
    path = Path("/proc") / str(pid)
    value = read(path / "cmdline", 16384).split(b"\0")
    need(
        value[:4]
        == [b"/opt/max-postiz/.venv/bin/python", b"-m", b"max_postiz_agent.server_cli", b"serve"],
        "FOREIGN_PROCESS",
    )
    status = (path / "status").read_text().splitlines()
    uids = next(line for line in status if line.startswith("Uid:")).split()[1:]
    need(
        all(v == "1000" for v in uids)
        and "NoNewPrivs:\t1" in status
        and "/max-postiz-editorial.service" in (path / "cgroup").read_text(),
        "PROCESS_OWNER_OR_CGROUP_CHANGED",
    )
    raw = (path / "stat").read_text()
    ticks = int(raw.rsplit(")", 1)[1].split()[19])
    boot = int(
        next(
            line.split()[1]
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    )
    return {
        "pid": pid,
        "start_ticks": ticks,
        "start_at": boot + ticks / os.sysconf("SC_CLK_TCK"),
        "command_sha256": sha(b"\0".join(value)),
    }


def profiles(ctx):
    need(
        ctx.get("binding") == BINDING
        and type(ctx.get("minimum_revision")) is int
        and ctx["minimum_revision"] > 0,
        "SOURCE_BINDING_INVALID",
    )
    values = ctx.get("profiles")
    need(isinstance(values, list) and len(values) == 1, "EXPLICIT_SOURCE_PROFILES_REQUIRED")
    for p in values:
        version = p.get("version")
        need(
            isinstance(version, str)
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            and re.fullmatch(r"[a-f0-9]{40}", p.get("source_commit", ""))
            and type(p.get("runtime_count")) is int
            and p["runtime_count"] >= 100
            and type(p.get("pid")) is int
            and p["pid"] > 1
            and type(p.get("start_ticks")) is int
            and p["start_ticks"] > 0
            and re.fullmatch("[a-f0-9]{64}", p.get("manifest_sha256", "")),
            "SOURCE_PROFILE_INVALID",
        )
        target = plain(p["manifest_path"])
        need(
            target.parent == ROOT / "data/reserve-20261026"
            and target.name.startswith("source-manifest-"),
            "MANIFEST_SCOPE_INVALID",
        )
    db = ctx.get("database")
    need(
        isinstance(db, dict) and set(db) == {"host", "port", "database", "user"},
        "EXPECTED_DATABASE_DESCRIPTOR_REQUIRED",
    )
    return values


def source_profile(ctx, identity):
    matches = []
    for p in profiles(ctx):
        if not process_matches(p, identity):
            continue
        # Explicit authorized profiles are alternatives, not ordered fallbacks.
        # A retained old manifest naturally fails its runtime bytes after upgrade.
        try:
            raw = read(p["manifest_path"], 4 * 1024**2)
            need(sha(raw) == p["manifest_sha256"], "MANIFEST_HASH_CHANGED")
            manifest = json.loads(raw)
            need(
                manifest.get("version") == p["version"]
                and manifest.get("source_commit") == p["source_commit"],
                "MANIFEST_IDENTITY_CHANGED",
            )
            runtime = {
                n: v
                for n, v in manifest.get("files", {}).items()
                if n == "pyproject.toml"
                or n.startswith(("src/", "schemas/", "prompts/", "migrations/"))
            }
            need(len(runtime) == p["runtime_count"], "RUNTIME_SCOPE_CHANGED")
            for name, meta in runtime.items():
                need(
                    not Path(name).is_absolute()
                    and ".." not in Path(name).parts
                    and sha(read(ROOT / name, 4 * 1024**2)) == meta["sha256"],
                    "RUNTIME_CODE_CHANGED",
                )
        except (Refused, OSError, ValueError, KeyError, TypeError):
            tick()
            continue
        matches.append((p, manifest))
    need(len(matches) == 1, "UNKNOWN_OR_AMBIGUOUS_SOURCE_INSTANCE")
    return matches[0]


def process_matches(profile, identity):
    return (
        identity.get("pid") == profile["pid"]
        and identity.get("start_ticks") == profile["start_ticks"]
    )


def literal_environment(initial, text, selected):
    # EXACT native cli.load_env semantics: literal strip + first setdefault;
    # quotes/interpolation are not interpreted, process environment wins.
    env = dict(initial)
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in selected:
                env.setdefault(key.strip(), value.strip())
    return env


def credentials(pid, ctx):
    # Literal process environment only. Never eval dotenv or send credentials to SSH caller.
    path = Path("/proc") / str(pid) / "environ"
    raw = path.read_bytes()
    need(len(raw) <= 256 * 1024, "ENVIRONMENT_BOUND")
    env = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if b"=" in item)
    # Python dotenv additions need not appear in proc's original environment.
    # Parse only literal assignments locally; never interpolate or eval values.
    names = ("EDITORIAL_INTERNAL_KEY", "EDITORIAL_OWNER_USER_ID", "EDITORIAL_OWNER_ORGANIZATION_ID")
    app = json.loads(read(ROOT / "config/app.json"))
    database_key = app.get("database_url_env", "DATABASE_URL")
    need(isinstance(database_key, str), "DATABASE_ENV_NAME_INVALID")
    env = literal_environment(
        env, read(ROOT / ".env.local", 1024 * 1024).decode("utf-8-sig"), {*names, database_key}
    )
    values = [env.get(k, "") for k in names]
    need(all(values) and len(values[0]) >= 32, "OWNER_AUTH_UNAVAILABLE")
    dsn = env.get(database_key, "")
    u = urlsplit(dsn)
    need(
        u.scheme in {"postgresql", "postgresql+psycopg"}
        and not u.query
        and not u.fragment
        and {"host": u.hostname, "port": u.port, "database": u.path.lstrip("/"), "user": u.username}
        == ctx["database"],
        "FOREIGN_DATABASE_BINDING",
    )
    return dict(
        zip(
            ("X-Editorial-Key", "X-Editorial-Owner", "X-Editorial-Organization"),
            values,
            strict=True,
        )
    ), values[0]


def get(path, headers):
    need(
        path
        in {
            "/health",
            "/internal/editorial/state",
            "/internal/editorial/routes",
            "/internal/editorial/routes?dispatch_filter=unresolved",
        },
        "UNSUPPORTED_READ_PATH",
    )
    tick()
    connection = http.client.HTTPConnection(
        "127.0.0.1", 8092, timeout=min(5, max(0.1, DEADLINE - time.monotonic()))
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                **headers,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache,no-store",
                "Pragma": "no-cache",
            },
        )
        response = connection.getresponse()
        need(response.status == 200, "OWNER_GET_UNAVAILABLE")
        need(
            response.getheader("Content-Type", "").split(";", 1)[0] == "application/json",
            "HTTP_CONTENT_TYPE_INVALID",
        )
        need(
            response.getheader("Content-Encoding", "identity").lower() == "identity",
            "ENCODING_FORBIDDEN",
        )
        length = response.getheader("Content-Length")
        need(
            length is None or length.isdigit() and int(length) <= 2 * 1024**2, "HTTP_RESPONSE_BOUND"
        )
        chunks = []
        total = 0
        while True:
            tick()
            block = response.read1(min(65536, 2 * 1024**2 + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            need(total <= 2 * 1024**2, "HTTP_RESPONSE_BOUND")
        body = b"".join(chunks)
        tick()
        need(len(body) <= 2 * 1024**2, "HTTP_RESPONSE_BOUND")
        value = json.loads(body)
        need(isinstance(value, dict), "HTTP_SCHEMA_INVALID")
        return value
    finally:
        connection.close()


def sanitizers(manifest, secret):
    name = "src/max_postiz_agent/monitor_reports.py"
    raw = read(ROOT / name)
    need(sha(raw) == manifest["files"][name]["sha256"], "SANITIZER_SOURCE_CHANGED")
    tree = ast.parse(raw.decode("utf-8-sig"))
    nodes = []
    namespace = {
        "math": math,
        "re": re,
        "unicodedata": unicodedata,
        "urlsplit": urlsplit,
        "os": types.SimpleNamespace(environ={**os.environ, "MONITOR_RO_API_KEY": secret}),
    }
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("_")
        ):
            try:
                namespace[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
        elif isinstance(node, ast.FunctionDef) and node.name in PURE:
            nodes.append(node)
    need({n.name for n in nodes} == PURE, "PURE_SANITIZER_CLOSURE_CHANGED")
    exec(  # noqa: S102 -- AST closure from the verified installed source
        compile(
            ast.Module(body=nodes, type_ignores=[]), "checked-native-monitor-sanitizers", "exec"
        ),
        namespace,
    )
    return namespace["_editorial"], namespace["_routes"]


def unavailable(ctx, code, maintenance=False):
    return {
        "contract": "remote-production-monitor-sample-v1",
        "binding": BINDING,
        "observed_at": time.time(),
        "status": "authorized_maintenance" if maintenance else "unavailable",
        "code": code,
        "source_identity": {
            "kind": "ssh-hostkey+service+manifest",
            "root": str(ROOT),
            "service": SERVICE,
        },
        "snapshot": {
            "schema": "monitor-snapshot-v1",
            "collected_at": time.time(),
            "read_only": True,
            "editorial": {
                "available": False,
                "paused": True,
                "heartbeat_at": None,
                "last_error": "MONITOR_STATE_UNAVAILABLE",
            },
            "routes": {
                "available": False,
                "error": "MONITOR_ROUTES_UNAVAILABLE",
                "targets": [],
                "dispatches": [],
            },
        },
    }


def sample(ctx):
    global DEADLINE
    DEADLINE = time.monotonic() + 25
    profiles(ctx)
    start = unit()
    pid = int(start["MainPID"])
    if start["ActiveState"] == "inactive" and pid == 0:
        m = ctx.get("maintenance", {})
        at = time.time()
        authorized = (
            m.get("authorized") is True
            and type(m.get("start")) in {int, float}
            and type(m.get("end")) in {int, float}
            and 0 < m["start"] <= at <= m["end"] <= m["start"] + 1800
        )
        return unavailable(
            ctx, "AUTHORIZED_HOST_MAINTENANCE" if authorized else "SOURCE_STOPPED", authorized
        )
    need(start["ActiveState"] == "active" and pid > 1, "SOURCE_NOT_RUNNING")
    identity = process(pid)
    profile, manifest = source_profile(ctx, identity)
    headers, secret = credentials(pid, ctx)
    need(
        (ROOT / "data/hourly-articles").is_dir() and (ROOT / "data/state").is_dir(),
        "SOURCE_STATE_ABSENT_NO_BOOTSTRAP",
    )
    health = get("/health", {})
    need(
        health
        == {
            "status": "ready",
            "version": profile["version"],
            "interface": "postiz_only",
            "worker_mode": "autonomous",
        },
        "FOREIGN_HEALTH",
    )
    state = get("/internal/editorial/state", headers)
    routes = get("/internal/editorial/routes", headers)
    need(
        state.get("version") == profile["version"]
        and type(state.get("revision")) is int
        and state["revision"] >= ctx["minimum_revision"]
        and type(state.get("paused")) is bool
        and isinstance(state.get("reserve"), dict)
        and isinstance(routes.get("targets"), list)
        and isinstance(routes.get("dispatches"), list)
        and isinstance(routes.get("pagination"), dict),
        "SOURCE_STATE_SCHEMA_INVALID",
    )
    visible = sum(row.get("state") in {"sending", "unknown"} for row in routes["dispatches"])
    if routes["pagination"].get("dispatch_active_count", 0) > visible:
        more = get("/internal/editorial/routes?dispatch_filter=unresolved", headers)
        need(
            isinstance(more.get("dispatches"), list) and isinstance(more.get("pagination"), dict),
            "UNRESOLVED_SCHEMA_INVALID",
        )
        merged = {row["id"]: row for row in routes["dispatches"]}
        merged.update({row["id"]: row for row in more["dispatches"]})
        routes["dispatches"] = list(merged.values())
        routes["pagination"]["unresolved_complete"] = not more["pagination"].get(
            "dispatch_has_next", True
        )
    else:
        routes["pagination"]["unresolved_complete"] = True
    after = get("/internal/editorial/state", headers)
    need(
        all(after.get(k) == state.get(k) for k in ("version", "revision", "paused"))
        and unit() == start
        and process(pid) == identity
        and sha(read(profile["manifest_path"])) == profile["manifest_sha256"],
        "SOURCE_CHANGED_DURING_SAMPLE",
    )
    editorial, route_clean = sanitizers(manifest, secret)
    return {
        "contract": "remote-production-monitor-sample-v1",
        "binding": BINDING,
        "status": "observed",
        "observed_at": time.time(),
        "source_identity": {
            "kind": "ssh-hostkey+service+manifest",
            "root": str(ROOT),
            "service": SERVICE,
            **identity,
            "installation_pid": profile["pid"],
            "version": profile["version"],
            "manifest_sha256": profile["manifest_sha256"],
            "source_commit": profile["source_commit"],
            "control_revision": state["revision"],
        },
        "snapshot": {
            "schema": "monitor-snapshot-v1",
            "collected_at": time.time(),
            "read_only": True,
            "editorial": editorial(state),
            "routes": route_clean(routes),
        },
    }


if __name__ == "__main__":
    context = globals().get("REMOTE_CONTEXT")
    if context is None:
        print(json.dumps({"state": "offline_only", "actions": 0}))
    else:
        try:
            result = sample(context)
        except BaseException as error:
            result = unavailable(
                context, str(error) if isinstance(error, Refused) else "SOURCE_READ_FAILED"
            )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
