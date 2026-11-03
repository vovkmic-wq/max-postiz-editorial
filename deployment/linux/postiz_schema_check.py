"""Metadata-only Prisma engine preflight for an immutable Postiz image.

The only Docker resource is a fresh stopped inert container. Nothing is started,
no database is contacted, and existing image engine bytes are never modified.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import select
import signal
import subprocess
import tarfile
import time
from urllib.parse import urlsplit
import uuid

CONTRACT = "postiz-prisma-image-engine-preflight-v1"
LABEL = "max-postiz.prisma-preflight"
IMAGE_LABEL = "max-postiz.prisma-preflight-image"
SUPPORTED_PRISMA = "6.5.0"
# Conservative image ABI contract, not a guess from the host OS or cache age.
# Expanding this table requires a separately verified image/engine compatibility case.
SUPPORTED_OPENSSL = {
    ("debian", "12"): "3.0.x",
    ("ubuntu", "22.04"): "3.0.x",
    ("ubuntu", "24.04"): "3.0.x",
}
MAX_METADATA = 4 * 1024**2
MAX_ENGINE = 64 * 1024**2
MAX_ARCHIVE = 256 * 1024**2
ENGINE_ENV = {
    "libquery-engine": "PRISMA_QUERY_ENGINE_LIBRARY",
    "schema-engine": "PRISMA_SCHEMA_ENGINE_BINARY",
}


class PreflightError(ValueError):
    """Safe operator-facing error code, without Docker output or credentials."""


def require(condition, code):
    if not condition:
        raise PreflightError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def plain(path):
    require(
        path.is_absolute() and not any(item.is_symlink() for item in (path, *path.parents)),
        "PRISMA_PRIVATE_PATH_INVALID",
    )
    return path


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class CommandStream:
    """Bounded reads from our own CLI process, honoring a caller's Docker wrapper."""

    def __init__(self, arguments, seconds):
        self.deadline = time.monotonic() + seconds
        self.total = 0
        self.process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def read(self, size):
        remaining = self.deadline - time.monotonic()
        require(
            remaining > 0 and select.select([self.process.stdout], [], [], remaining)[0],
            "PRISMA_DOCKER_COMMAND_TIMEOUT",
        )
        raw = os.read(self.process.stdout.fileno(), min(size, 65536))
        self.total += len(raw)
        require(self.total <= MAX_ARCHIVE, "PRISMA_ARCHIVE_TOO_LARGE")
        return raw

    def finish(self):
        require(
            self.process.wait(timeout=max(0.01, self.deadline - time.monotonic())) == 0,
            "PRISMA_DOCKER_COMMAND_FAILED",
        )

    def close(self):
        try:
            if self.process.poll() is None:
                # Only this start_new_session child group; never a Docker resource.
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=1)
        finally:
            self.process.stdout.close()


class DockerCLI:
    @contextlib.contextmanager
    def command(self, arguments, seconds=30):
        stream = CommandStream(["docker", "--context", "default", *arguments], seconds)
        try:
            yield stream
            stream.finish()
        finally:
            stream.close()

    def run(self, arguments):
        with self.command(arguments) as stream:
            raw = bytearray()
            while block := stream.read(min(65536, MAX_METADATA + 1 - len(raw))):
                raw.extend(block)
                require(len(raw) <= MAX_METADATA, "PRISMA_DOCKER_METADATA_TOO_LARGE")
            return bytes(raw)

    @classmethod
    def local_default(cls):
        require(
            not any(
                os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "BUILDKIT_HOST")
            ),
            "PRISMA_AMBIENT_DOCKER_FORBIDDEN",
        )
        client = cls()
        value = client.run(
            ["context", "inspect", "default", "--format", "{{json .Endpoints.docker.Host}}"]
        )
        require(len(value) <= 4096, "PRISMA_DOCKER_CONTEXT_UNAVAILABLE")
        endpoint = urlsplit(json.loads(value))
        require(
            endpoint.scheme == "unix"
            and not endpoint.netloc
            and endpoint.path.startswith("/")
            and not endpoint.query
            and not endpoint.fragment,
            "PRISMA_LOCAL_UNIX_DOCKER_REQUIRED",
        )
        return client

    def image(self, image):
        return json.loads(self.run(["image", "inspect", "--format", "{{json .}}", image]))

    def create(self, image, name, run_id):
        # Explicit compatible per-container logging; never change daemon defaults.
        value = (
            self.run(
                [
                    "container",
                    "create",
                    "--name",
                    name,
                    "--label",
                    LABEL + "=" + run_id,
                    "--label",
                    IMAGE_LABEL + "=" + image,
                    "--network",
                    "none",
                    "--read-only",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--log-driver",
                    "local",
                    "--log-opt",
                    "max-size=1m",
                    "--log-opt",
                    "max-file=1",
                    "--log-opt",
                    "compress=false",
                    "--entrypoint",
                    "/bin/true",
                    image,
                    "/bin/true",
                ]
            )
            .decode("ascii")
            .strip()
        )
        require(re.fullmatch(r"[a-f0-9]{64}", value), "PRISMA_CONTAINER_ID_INVALID")
        return value

    def pending(self, image):
        result = (
            self.run(
                [
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "label=" + LABEL,
                    "--filter",
                    "label=" + IMAGE_LABEL + "=" + image,
                    "--format",
                    "{{.ID}}",
                ]
            )
            .decode("ascii")
            .splitlines()
        )
        require(
            all(re.fullmatch(r"[a-f0-9]{64}", item) for item in result),
            "PRISMA_PENDING_CONTAINER_QUERY_FAILED",
        )
        return result

    def inspect(self, container):
        return json.loads(self.run(["container", "inspect", "--format", "{{json .}}", container]))

    def remove(self, container):
        # v removes only anonymous volumes created for this never-started container.
        self.run(["container", "rm", "--volumes", container])

    @contextlib.contextmanager
    def archive(self, container, path, deadline):
        with self.command(
            ["container", "cp", container + ":" + path, "-"],
            seconds=max(0.01, deadline - time.monotonic()),
        ) as stream:
            yield stream
            # tar may stop before cp's final padding; drain our bounded stream so
            # a small pipe cannot block the child's exit after a valid archive.
            while stream.read(65536):
                pass


class LimitedReader:
    def __init__(self, source, deadline):
        self.source, self.deadline, self.total = source, deadline, 0

    def read(self, size=-1):
        require(time.monotonic() <= self.deadline, "PRISMA_METADATA_TIMEOUT")
        size = min(size if size >= 0 else MAX_ARCHIVE + 1, MAX_ARCHIVE + 1 - self.total)
        raw = self.source.read(size)
        self.total += len(raw)
        require(self.total <= MAX_ARCHIVE, "PRISMA_ARCHIVE_TOO_LARGE")
        return raw


def member_path(member):
    path = PurePosixPath(member.name)
    require(
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in member.name
        and "\x00" not in member.name
        and len(path.parts) <= 8,
        "PRISMA_ARCHIVE_PATH_INVALID",
    )
    return path


def image_file(client, container, path, deadline, limit=MAX_METADATA):
    """Follow bounded in-image package links; never extract or follow tar links locally."""
    for _ in range(8):
        require(
            path.startswith("/app/node_modules/")
            or path in {"/etc/os-release", "/usr/lib/os-release"},
            "PRISMA_METADATA_PATH_FORBIDDEN",
        )
        with client.archive(container, path, deadline) as source:
            with tarfile.open(fileobj=LimitedReader(source, deadline), mode="r|*") as archive:
                raw, link, files = None, None, 0
                for index, item in enumerate(archive):
                    require(index < 16, "PRISMA_METADATA_ENTRY_LIMIT")
                    name = member_path(item)
                    if item.isdir():
                        continue
                    require(
                        name.name == PurePosixPath(path).name, "PRISMA_METADATA_FILENAME_CHANGED"
                    )
                    files += 1
                    require(files == 1, "PRISMA_SINGLE_METADATA_FILE_REQUIRED")
                    if item.issym():
                        link = posixpath.normpath(
                            posixpath.join(posixpath.dirname(path), item.linkname)
                        )
                        continue
                    require(
                        item.isfile() and 0 < item.size <= limit, "PRISMA_METADATA_FILE_INVALID"
                    )
                    stream = archive.extractfile(item)
                    require(stream is not None, "PRISMA_METADATA_FILE_INVALID")
                    raw = stream.read(limit + 1)
                    require(
                        len(raw) == item.size and len(raw) <= limit, "PRISMA_METADATA_FILE_INVALID"
                    )
                require(files == 1, "PRISMA_SINGLE_METADATA_FILE_REQUIRED")
                if link is None:
                    return raw
                path = link
    raise PreflightError("PRISMA_METADATA_LINK_LIMIT")


def supported_metadata(package_raw, engines_raw, cli_raw):
    package, engines = json.loads(package_raw), json.loads(engines_raw)
    require(
        package.get("name") == "prisma"
        and engines.get("name") == "@prisma/engines"
        and package.get("version") == engines.get("version") == SUPPORTED_PRISMA,
        "PRISMA_VERSION_UNSUPPORTED",
    )
    version = engines.get("dependencies", {}).get("@prisma/engines-version", "")
    commits = re.findall(r"(?<![a-f0-9])[a-f0-9]{40}(?![a-f0-9])", version)
    require(len(commits) == 1, "PRISMA_ENGINE_COMMIT_MISSING")
    cli = cli_raw.decode("utf-8")
    require(
        '"libquery-engine":"PRISMA_QUERY_ENGINE_LIBRARY"' in cli
        and '"schema-engine":"PRISMA_SCHEMA_ENGINE_BINARY"' in cli
        and re.search(
            r"if\((\w+)\.envVarPath&&[\w.]+\.existsSync\(\1\.envVarPath\)\)return!1;", cli
        ),
        "PRISMA_ENGINE_ENV_CONTRACT_UNSUPPORTED",
    )
    return {
        "prisma_version": SUPPORTED_PRISMA,
        "engine_commit": commits[0],
        "cli_sha256": sha(cli_raw),
        "package_sha256": sha(package_raw),
        "engines_package_sha256": sha(engines_raw),
    }


def platform_matches(platform, architecture, release):
    fields = dict(
        line.split("=", 1) for line in release.decode("utf-8").splitlines() if "=" in line
    )
    distribution, version = (fields.get(key, "").strip('"') for key in ("ID", "VERSION_ID"))
    require(architecture in {"amd64", "arm64"}, "PRISMA_IMAGE_ARCHITECTURE_UNSUPPORTED")
    abi = SUPPORTED_OPENSSL.get((distribution, version))
    require(abi is not None, "PRISMA_IMAGE_OPENSSL_ABI_UNSUPPORTED")
    prefix = "linux-arm64-openssl-" if architecture == "arm64" else "debian-openssl-"
    return platform == prefix + abi


def cache_root(image):
    environment = dict(
        item.split("=", 1) for item in image.get("Config", {}).get("Env", []) if "=" in item
    )
    require(
        environment.get("PRISMA_CLI_QUERY_ENGINE_TYPE", "library") in {"", "library"},
        "PRISMA_QUERY_ENGINE_TYPE_UNSUPPORTED",
    )
    home = environment.get("HOME") or (
        "/root" if image.get("Config", {}).get("User", "") in {"", "root", "0", "0:0"} else ""
    )
    base = environment.get("XDG_CACHE_HOME") or (home + "/.cache" if home else "")
    require(
        base.startswith("/")
        and ".." not in PurePosixPath(base).parts
        and re.fullmatch(r"/[A-Za-z0-9_./-]+", base),
        "PRISMA_IMAGE_CACHE_HOME_UNSUPPORTED",
    )
    return base.rstrip("/") + "/prisma/master"


def discover_engines(client, container, cache, metadata, architecture, release, deadline):
    commit = metadata["engine_commit"]
    found, declared_bytes = {}, 0
    with client.archive(container, cache + "/" + commit, deadline) as source:
        with tarfile.open(fileobj=LimitedReader(source, deadline), mode="r|*") as archive:
            for index, item in enumerate(archive):
                require(index < 4096, "PRISMA_CACHE_ENTRY_LIMIT")
                parts = member_path(item).parts
                require(not item.issym() and not item.islnk(), "PRISMA_CACHE_LINK_FORBIDDEN")
                declared_bytes += item.size
                require(
                    0 <= item.size <= MAX_ARCHIVE and declared_bytes <= MAX_ARCHIVE,
                    "PRISMA_CACHE_EXPANDED_SIZE_LIMIT",
                )
                if item.isdir():
                    continue
                require(item.isfile(), "PRISMA_CACHE_ENTRY_INVALID")
                if (
                    len(parts) != 3
                    or parts[0] != commit
                    or not platform_matches(parts[1], architecture, release)
                ):
                    continue
                platform, name = parts[1:]
                if name not in {*ENGINE_ENV, *(key + ".sha256" for key in ENGINE_ENV)}:
                    continue
                require(
                    0 < item.size <= (256 if name.endswith(".sha256") else MAX_ENGINE),
                    "PRISMA_ENGINE_SIZE_INVALID",
                )
                entries = found.setdefault(platform, {})
                require(name not in entries, "PRISMA_DUPLICATE_ENGINE_ENTRY")
                stream = archive.extractfile(item)
                require(stream is not None, "PRISMA_ENGINE_FILE_INVALID")
                digest, size, checksum = hashlib.sha256(), 0, bytearray()
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
                    if name.endswith(".sha256"):
                        checksum.extend(block)
                require(size == item.size, "PRISMA_ENGINE_TRUNCATED")
                entries[name] = {
                    "path": cache + "/" + "/".join(parts),
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "mode": item.mode,
                }
                if checksum:
                    entries[name]["checksum"] = checksum.decode("ascii").strip()
    pairs = []
    for platform, entries in found.items():
        require(
            set(entries) == {*ENGINE_ENV, *(key + ".sha256" for key in ENGINE_ENV)},
            "PRISMA_ENGINE_PAIR_INCOMPLETE",
        )
        for name in ENGINE_ENV:
            expected = entries[name + ".sha256"]["checksum"]
            require(
                re.fullmatch(r"[a-f0-9]{64}", expected) and entries[name]["sha256"] == expected,
                "PRISMA_ENGINE_CHECKSUM_MISMATCH",
            )
        require(entries["schema-engine"]["mode"] & 0o111, "PRISMA_SCHEMA_ENGINE_NOT_EXECUTABLE")
        pairs.append(
            {"binary_target": platform, "engines": {name: entries[name] for name in ENGINE_ENV}}
        )
    require(len(pairs) == 1, "PRISMA_ENGINE_PLATFORM_AMBIGUOUS_OR_MISSING")
    return pairs[0]


def owned_inert(client, container, image_id, run_id):
    value = client.inspect(container)
    require(
        value.get("Id") == container
        and value.get("Config", {}).get("Image") == image_id
        and value["Config"].get("Labels", {}).get(LABEL) == run_id
        and value["Config"].get("Entrypoint") == ["/bin/true"]
        and value.get("State", {}).get("Status") == "created"
        and value["State"].get("Running") is False
        and value["State"].get("Pid") == 0
        and value.get("HostConfig", {}).get("NetworkMode") == "none"
        and value["HostConfig"].get("ReadonlyRootfs") is True
        and not value["HostConfig"].get("PortBindings")
        and not value["HostConfig"].get("Binds")
        and not value["HostConfig"].get("VolumesFrom"),
        "PRISMA_OWNED_INERT_CONTAINER_CHANGED",
    )
    return value


def inspect_image(client, image_id, folder, run_id, seconds=180):
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", image_id), "PRISMA_IMMUTABLE_IMAGE_REQUIRED")
    image = client.image(image_id)
    require(
        image.get("Id") == image_id and image.get("Os") == "linux",
        "PRISMA_LOADED_LINUX_IMAGE_REQUIRED",
    )
    require(not client.pending(image_id), "PRISMA_EXISTING_INSPECTION_REQUIRES_RECONCILIATION")
    deadline = time.monotonic() + seconds
    name = "postiz-prisma-preflight-" + uuid.UUID(run_id).hex
    report = {
        "contract": CONTRACT,
        "state": "incomplete",
        "image_id": image_id,
        "run_id": run_id,
        "container_name": name,
        "container_started": False,
        "database_connections": 0,
        "engine_downloads": 0,
        "cache_writes": 0,
        "cleanup_confirmed": False,
    }
    private_json(folder / "claim.json", {**report, "at": time.time(), "automatic_retry": False})
    container = None
    try:
        container = client.create(image_id, name, run_id)
        report["container_id"] = container
        actual = owned_inert(client, container, image_id, run_id)
        report["container_image_id"] = actual.get("Image")
        package = image_file(client, container, "/app/node_modules/prisma/package.json", deadline)
        engines = image_file(
            client, container, "/app/node_modules/@prisma/engines/package.json", deadline
        )
        cli = image_file(client, container, "/app/node_modules/prisma/build/index.js", deadline)
        metadata = supported_metadata(package, engines, cli)
        release = image_file(client, container, "/etc/os-release", deadline, limit=65536)
        pair = discover_engines(
            client,
            container,
            cache_root(image),
            metadata,
            image.get("Architecture"),
            release,
            deadline,
        )
        environment = {ENGINE_ENV[name]: item["path"] for name, item in pair["engines"].items()}
        report.update(metadata, **pair, engine_environment=environment)
        env_path = folder / "engine.env"
        descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("".join(key + "=" + value + "\n" for key, value in environment.items()))
            stream.flush()
            os.fsync(stream.fileno())
        report["engine_env_sha256"] = sha((folder / "engine.env").read_bytes())
        report["state"] = "verified_image_engine_metadata"
    except (
        PreflightError,
        OSError,
        ValueError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        report["safe_error_code"] = (
            str(error) if isinstance(error, PreflightError) else "PRISMA_PREFLIGHT_FAILED"
        )
    finally:
        if container is not None:
            try:
                owned_inert(client, container, image_id, run_id)
                client.remove(container)
                report["cleanup_confirmed"] = True
            except (PreflightError, OSError, ValueError, subprocess.SubprocessError):
                report.update(
                    state="incomplete", safe_error_code="PRISMA_OWNED_CLEANUP_UNCONFIRMED"
                )
        report["finished_at"] = time.time()
        private_json(folder / "report.json", report)
    return report


def engine_environment(report):
    require(
        report.get("contract") == CONTRACT
        and report.get("state") == "verified_image_engine_metadata"
        and report.get("cleanup_confirmed") is True,
        "PRISMA_VERIFIED_METADATA_REQUIRED",
    )
    environment = report.get("engine_environment", {})
    require(set(environment) == set(ENGINE_ENV.values()), "PRISMA_BOTH_ENGINE_OVERRIDES_REQUIRED")
    return dict(environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-image", required=True)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data/deployment-schema-checks",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    folder = None
    try:
        require(
            os.name == "posix" and 30 <= args.timeout_seconds <= 900,
            "PRISMA_LINUX_BOUNDED_CHECK_REQUIRED",
        )
        os.umask(0o077)
        evidence = plain(args.evidence_root.absolute())
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(evidence.stat().st_mode & 0o077 == 0, "PRISMA_PRIVATE_EVIDENCE_REQUIRED")
        run_id = str(uuid.uuid4())
        folder = evidence / run_id
        folder.mkdir(mode=0o700)
        sync_directory(evidence)
        report = inspect_image(
            DockerCLI.local_default(), args.inspect_image, folder, run_id, args.timeout_seconds
        )
        path = folder / "report.json"
        print(
            json.dumps(
                {
                    "contract": CONTRACT,
                    "state": report["state"],
                    "image_id": args.inspect_image,
                    "report": str(path),
                    "report_sha256": sha(path.read_bytes()),
                    "engine_env_file": str(folder / "engine.env")
                    if report["state"] == "verified_image_engine_metadata"
                    else None,
                    "safe_error_code": report.get("safe_error_code"),
                }
            )
        )
        return (
            0
            if report["state"] == "verified_image_engine_metadata" and report["cleanup_confirmed"]
            else 2
        )
    except (PreflightError, OSError, ValueError, subprocess.SubprocessError):
        print("PRISMA_PREFLIGHT_FAILED_RECONCILE_PRIVATE_EVIDENCE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
