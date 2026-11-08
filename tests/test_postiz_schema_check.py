"""Immutable-image metadata fixtures; no Docker, database, network or real Prisma."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "postiz_schema_check", ROOT / "deployment/linux/postiz_schema_check.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
IMAGE = "sha256:" + "1" * 64
CONTAINER = "2" * 64
COMMIT = "3" * 40
RUN = "d2aa2474-4a65-46a5-a1bc-9b060c11d39f"
CLI = (
    b'"libquery-engine":"PRISMA_QUERY_ENGINE_LIBRARY","schema-engine":"PRISMA_SCHEMA_ENGINE_BINARY";'
    b'async function check(e){if(e.envVarPath&&fs.existsSync(e.envVarPath))return!1;throw "cache EROFS"}'
)


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as target:
        for name, raw, kind in entries:
            item = tarfile.TarInfo(name)
            item.mode = 0o755
            if kind == "link":
                item.type = tarfile.SYMTYPE
                item.linkname = raw.decode()
                target.addfile(item)
            else:
                item.size = len(raw)
                target.addfile(item, io.BytesIO(raw))
    return output.getvalue()


def cache(platforms=("debian-openssl-3.0.x",), corrupt=False, missing=False):
    entries = []
    for platform in platforms:
        for name in ("libquery-engine", "schema-engine"):
            if missing and name == "schema-engine":
                continue
            raw = (name + "-synthetic-engine").encode()
            prefix = COMMIT + "/" + platform + "/" + name
            entries.append((prefix, raw, "file"))
            checksum = "0" * 64 if corrupt else hashlib.sha256(raw).hexdigest()
            entries.append((prefix + ".sha256", checksum.encode(), "file"))
    return archive(entries)


class Docker:
    def __init__(self):
        self.run_id = None
        self.created = []
        self.removed = []
        self.reads = []
        self.pending_containers = []
        self.changed_cleanup = False
        self.metadata = {
            "/app/node_modules/prisma/package.json": archive(
                [
                    (
                        "package.json",
                        json.dumps({"name": "prisma", "version": "6.5.0"}).encode(),
                        "file",
                    )
                ]
            ),
            "/app/node_modules/@prisma/engines/package.json": archive(
                [
                    (
                        "package.json",
                        json.dumps(
                            {
                                "name": "@prisma/engines",
                                "version": "6.5.0",
                                "dependencies": {"@prisma/engines-version": "6.5.0-1." + COMMIT},
                            }
                        ).encode(),
                        "file",
                    )
                ]
            ),
            "/app/node_modules/prisma/build/index.js": archive([("index.js", CLI, "file")]),
            "/etc/os-release": archive([("os-release", b'ID=debian\nVERSION_ID="12"\n', "file")]),
            "/root/.cache/prisma/master/" + COMMIT: cache(),
        }

    def image(self, image):
        return {
            "Id": image,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"Env": ["HOME=/root"], "User": ""},
        }

    def pending(self, image):
        return self.pending_containers

    def create(self, image, name, run_id):
        self.created.append((image, name, run_id))
        self.run_id = run_id
        return CONTAINER

    def inspect(self, container):
        return {
            "Id": container,
            "Image": IMAGE,
            "Config": {
                "Image": IMAGE,
                "Entrypoint": ["/bin/true"],
                "Labels": {m.LABEL: self.run_id},
            },
            "State": {
                "Status": "created",
                "Running": self.changed_cleanup and len(self.reads) > 0,
                "Pid": 0,
            },
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "PortBindings": None},
        }

    def remove(self, container):
        self.removed.append(container)

    @contextlib.contextmanager
    def archive(self, container, path, deadline):
        assert container == CONTAINER
        self.reads.append((container, path))
        if path not in self.metadata:
            raise m.PreflightError("PRISMA_IMAGE_METADATA_MISSING")
        yield io.BytesIO(self.metadata[path])


@pytest.fixture
def folder(tmp_path, monkeypatch):
    # Windows does not implement Linux directory fsync. Only the fsync call is
    # stubbed; all tar/JSON/hash/engine selection and owned cleanup gates run.
    monkeypatch.setattr(m, "sync_directory", lambda path: None)
    return tmp_path


def test_readonly_cache_discovery_sets_both_existing_overrides_without_start_or_download(folder):
    docker = Docker()
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert report["state"] == "verified_image_engine_metadata" and report["cleanup_confirmed"]
    environment = m.engine_environment(report)
    assert set(environment) == {"PRISMA_QUERY_ENGINE_LIBRARY", "PRISMA_SCHEMA_ENGINE_BINARY"}
    assert all(
        value.startswith("/root/.cache/prisma/master/" + COMMIT) for value in environment.values()
    )
    assert (folder / "engine.env").read_text().splitlines() == [
        key + "=" + value for key, value in environment.items()
    ]
    assert (
        report["cache_writes"] == report["engine_downloads"] == report["database_connections"] == 0
    )
    assert report["container_started"] is False and docker.removed == [CONTAINER]
    assert {value[0] for value in docker.reads} == {CONTAINER}
    assert report["engine_commit"] == COMMIT  # discovered, never a fixed VM commit/hash


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("missing", "PRISMA_ENGINE_PAIR_INCOMPLETE"),
        ("corrupt", "PRISMA_ENGINE_CHECKSUM_MISMATCH"),
        ("duplicate", "PRISMA_DUPLICATE_ENGINE_ENTRY"),
    ],
)
def test_missing_corrupt_or_duplicate_cache_fails_before_env_and_cleans(folder, kind, expected):
    docker = Docker()
    docker.metadata["/root/.cache/prisma/master/" + COMMIT] = cache(
        platforms=("debian-openssl-3.0.x", "debian-openssl-3.0.x")
        if kind == "duplicate"
        else ("debian-openssl-3.0.x",),
        corrupt=kind == "corrupt",
        missing=kind == "missing",
    )
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert report["state"] == "incomplete" and report["safe_error_code"] == expected
    assert not (folder / "engine.env").exists() and report["cleanup_confirmed"]
    assert docker.removed == [CONTAINER]


def test_single_valid_checksummed_wrong_openssl_abi_is_not_compatible(folder):
    docker = Docker()
    docker.metadata["/root/.cache/prisma/master/" + COMMIT] = cache(
        platforms=("debian-openssl-1.1.x",)
    )
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert report["state"] == "incomplete"
    assert report["safe_error_code"] == "PRISMA_ENGINE_PLATFORM_AMBIGUOUS_OR_MISSING"
    assert not (folder / "engine.env").exists() and docker.removed == [CONTAINER]


@pytest.mark.parametrize(
    "package,cli,expected",
    [
        ({"name": "prisma", "version": "7.0.0"}, CLI, "PRISMA_VERSION_UNSUPPORTED"),
        (
            {"name": "prisma", "version": "6.5.0"},
            b"unsupported CLI",
            "PRISMA_ENGINE_ENV_CONTRACT_UNSUPPORTED",
        ),
    ],
)
def test_unknown_prisma_or_cli_override_contract_never_falls_back_to_download(
    folder, package, cli, expected
):
    docker = Docker()
    docker.metadata["/app/node_modules/prisma/package.json"] = archive(
        [("package.json", json.dumps(package).encode(), "file")]
    )
    docker.metadata["/app/node_modules/prisma/build/index.js"] = archive(
        [("index.js", cli, "file")]
    )
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert report["safe_error_code"] == expected and report["state"] == "incomplete"
    assert not any("/master/" in path for _, path in docker.reads)
    assert docker.removed == [CONTAINER]


def test_cache_tar_traversal_and_symlinks_are_rejected_without_extraction(folder):
    docker = Docker()
    docker.metadata["/root/.cache/prisma/master/" + COMMIT] = archive(
        [("../outside", b"data", "file")]
    )
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert report["safe_error_code"] == "PRISMA_ARCHIVE_PATH_INVALID"
    assert not (folder.parent / "outside").exists()


def test_package_symlink_escape_never_reads_arbitrary_image_file(folder):
    docker = Docker()
    docker.metadata["/app/node_modules/prisma/package.json"] = archive(
        [("package.json", b"/etc/shadow", "link")]
    )
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert report["safe_error_code"] == "PRISMA_METADATA_PATH_FORBIDDEN"
    assert not any(path == "/etc/shadow" for _, path in docker.reads)


def test_same_image_engine_commit_and_checksum_metadata_are_both_required():
    package = json.dumps({"name": "prisma", "version": "6.5.0"}).encode()
    engines = json.dumps(
        {"name": "@prisma/engines", "version": "6.5.0", "dependencies": {}}
    ).encode()
    with pytest.raises(m.PreflightError, match="PRISMA_ENGINE_COMMIT_MISSING"):
        m.supported_metadata(package, engines, CLI)


def test_platform_selection_is_image_architecture_and_distribution_specific():
    assert m.platform_matches("linux-arm64-openssl-3.0.x", "arm64", b"ID=ubuntu\nVERSION_ID=22.04")
    assert not m.platform_matches("debian-openssl-3.0.x", "arm64", b"ID=ubuntu\nVERSION_ID=24.04")
    assert not m.platform_matches("debian-openssl-1.1.x", "amd64", b"ID=debian\nVERSION_ID=12")
    with pytest.raises(m.PreflightError, match="PRISMA_IMAGE_OPENSSL_ABI_UNSUPPORTED"):
        m.platform_matches("unknown", "amd64", b"ID=unknown\nVERSION_ID=1")


def test_cache_home_comes_from_image_metadata_and_binary_mode_fails_closed():
    image = {"Config": {"Env": ["HOME=/home/node"], "User": "1000"}}
    assert m.cache_root(image) == "/home/node/.cache/prisma/master"
    image["Config"]["Env"].append("PRISMA_CLI_QUERY_ENGINE_TYPE=binary")
    with pytest.raises(m.PreflightError, match="PRISMA_QUERY_ENGINE_TYPE_UNSUPPORTED"):
        m.cache_root(image)


def test_unknown_prior_container_blocks_replacement_inspection(folder):
    docker = Docker()
    docker.pending_containers = [{"Id": CONTAINER}]
    with pytest.raises(
        m.PreflightError, match="PRISMA_EXISTING_INSPECTION_REQUIRES_RECONCILIATION"
    ):
        m.inspect_image(docker, IMAGE, folder, RUN)
    assert docker.created == [] and docker.removed == []


def test_cleanup_never_stops_or_removes_container_that_started_unknown(folder):
    docker = Docker()
    docker.changed_cleanup = True
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert (
        report["state"] == "incomplete" and not report["cleanup_confirmed"] and docker.removed == []
    )
    with pytest.raises(m.PreflightError, match="PRISMA_VERIFIED_METADATA_REQUIRED"):
        m.engine_environment(report)


def test_create_timeout_keeps_claim_and_never_retries_or_claims_cleanup(folder, monkeypatch):
    docker = Docker()
    calls = []

    def create(*args):
        calls.append(args)
        raise TimeoutError("synthetic outcome unknown")

    monkeypatch.setattr(docker, "create", create)
    report = m.inspect_image(docker, IMAGE, folder, RUN)
    assert len(calls) == 1 and (folder / "claim.json").is_file()
    assert report["state"] == "incomplete" and not report["cleanup_confirmed"]


def test_inherited_compression_true_is_overridden_for_owned_max_file_one(monkeypatch):
    docker = m.DockerCLI()
    captured = []

    def run(arguments):
        captured.append(arguments)
        options = {"compress": "true"}
        for index, argument in enumerate(arguments):
            if argument == "--log-opt":
                key, value = arguments[index + 1].split("=", 1)
                options[key] = value
        assert options["max-file"] == "1" and options["compress"] == "false"
        return CONTAINER.encode()

    monkeypatch.setattr(docker, "run", run)
    assert docker.create(IMAGE, "owned", RUN) == CONTAINER
    assert "--read-only" in captured[0]
    assert captured[0][captured[0].index("--network") + 1] == "none"
    assert captured[0][captured[0].index("--entrypoint") + 1] == "/bin/true"


def test_release_requires_metadata_preflight_before_any_compose_recreate():
    script = (ROOT / "deployment/linux/postiz-release.sh").read_text()
    assert script.index("verify-three-platform-image.py") < script.index(
        "check-postiz-schema.py --inspect-image"
    )
    assert script.index("check-postiz-schema.py --inspect-image") < script.index("compose=(docker")
    assert "set -euo pipefail" in script and "--pull never postiz" in script


def test_default_local_context_uses_cli_permission_wrapper_without_opening_socket(monkeypatch):
    captured = []
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "BUILDKIT_HOST"):
        monkeypatch.delenv(key, raising=False)

    def run(self, arguments):
        captured.append(arguments)
        return b'"unix:///var/run/docker.sock"'

    monkeypatch.setattr(m.DockerCLI, "run", run)
    assert isinstance(m.DockerCLI.local_default(), m.DockerCLI)
    assert captured == [
        ["context", "inspect", "default", "--format", "{{json .Endpoints.docker.Host}}"]
    ]
    monkeypatch.setenv("DOCKER_HOST", "tcp://unowned:2375")
    with pytest.raises(m.PreflightError, match="PRISMA_AMBIENT_DOCKER_FORBIDDEN"):
        m.DockerCLI.local_default()
    assert len(captured) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe select and owned process-group cleanup")
def test_stalled_owned_cli_stream_times_out_and_closes_only_its_child():
    stream = m.CommandStream([sys.executable, "-c", "import time; time.sleep(30)"], seconds=0.1)
    try:
        with pytest.raises(m.PreflightError, match="PRISMA_DOCKER_COMMAND_TIMEOUT"):
            stream.read(1)
    finally:
        stream.close()
    assert stream.process.poll() is not None
