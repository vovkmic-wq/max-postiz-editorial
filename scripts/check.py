"""Единая локальная проверка без запуска редакции и чтения runtime-секретов."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
LOCKS = (
    "requirements.lock",
    "requirements-dev.lock",
    "requirements-browser.lock",
    "requirements-deployment.lock",
)
WHEEL_CASES = (
    "nasa_links_v1",
    "science_mailto_isolation",
    "readback_runtime_api",
    "readback_wheel_proof",
)
WHEEL_PROOF_PROBE = r"""
import contextlib, hashlib, io, json, os, runpy, sys, sysconfig
from pathlib import Path
site, archive, verifier, repository, build_source = (Path(p).resolve() for p in sys.argv[1:])
forbidden_roots = (repository / 'src', repository / 'scripts', build_source)
def audit(event, args):
    if (event in {'subprocess.Popen', 'os.system', 'os.fork', 'os.forkpty',
                  'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'}
            or event.startswith(('os.exec', 'os.spawn', 'os.posix_spawn', 'os.startfile',
                                 'socket.connect', 'socket.bind', 'socket.send'))):
        raise AssertionError('Wheel proof cannot access network or start processes')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in forbidden_roots):
            raise AssertionError('Wheel proof cannot read repository/build source')
sys.addaudithook(audit)
# The editable development distribution is deliberately excluded: this process
# checks the fresh wheel alone, and does not import application dependencies.
development_sites = {Path(sysconfig.get_path(name)).resolve() for name in ('purelib', 'platlib')}
sys.path[:] = [p for p in sys.path if Path(p).resolve() not in development_sites]
sys.path.insert(0, str(site))
verify = runpy.run_path(str(verifier))['verify_installed_wheel']
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
proof = verify(archive, digest)
assert proof['sha256'] == digest and proof['runtime_files_verified'] > 0
try:
    verify(archive, '0' * 64 if digest != '0' * 64 else '1' * 64)
except ValueError as exc:
    assert str(exc) == 'WHEEL_SHA256_MISMATCH'
else:
    raise AssertionError('Incorrect wheel digest accepted')
target = site / 'max_postiz_agent' / '__init__.py'
original = target.read_bytes()
try:
    target.write_bytes(original + b'\n# tampered for isolated check\n')
    try:
        verify(archive, digest)
    except ValueError as exc:
        assert str(exc) == 'INSTALLED_PACKAGE_BYTES_MISMATCH'
    else:
        raise AssertionError('Modified installed package accepted')
finally:
    target.write_bytes(original)
assert 'max_postiz_agent' not in sys.modules
sys.path.insert(0, str(verifier.parent))
sys.argv = [str(verifier.with_name('readback_max.py')), '--help']
help_output = io.StringIO()
try:
    with contextlib.redirect_stdout(help_output):
        runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError('Readback help did not exit')
assert '--wheel-sha256' in help_output.getvalue()
assert 'max_postiz_agent' not in sys.modules
print(json.dumps({'contract': 'installed-readback-wheel-proof-v1', 'passed': True}))
"""
WHEEL_PROBE = r"""
import json, os, runpy, socket, subprocess, sys
from pathlib import Path
wheel = Path(sys.argv[1]).resolve()
repository, build_source = (Path(p).resolve() for p in sys.argv[3:5])
forbidden_roots = (repository / 'src', repository / 'scripts', build_source)
assert not any(n == 'max_postiz_agent' or n.startswith('max_postiz_agent.') for n in sys.modules)
sys.path.insert(0, str(wheel))
attempted = []
def audit(event, args):
    if (event in {'subprocess.Popen', 'os.system', 'os.fork', 'os.forkpty',
                  'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'}
            or event.startswith(('os.exec', 'os.spawn', 'os.posix_spawn', 'os.startfile',
                                 'socket.connect', 'socket.bind', 'socket.send'))):
        attempted.append(True)
        raise AssertionError('Installed wheel probe cannot access network or start processes')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == 'fetch_nasa_article.py' or any(path.is_relative_to(p) for p in forbidden_roots):
            attempted.append(True)
            raise AssertionError('Installed wheel cannot read repository/build source')
sys.addaudithook(audit)
def forbidden(*args, **kwargs):
    attempted.append(True)
    raise AssertionError('Installed wheel probe cannot access network or start processes')
socket.getaddrinfo = socket.create_connection = forbidden
socket.socket.connect = socket.socket.connect_ex = socket.socket.bind = forbidden
socket.socket.sendto = forbidden
class ForbiddenProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        forbidden()
subprocess.Popen = ForbiddenProcess
os.system = forbidden
from max_postiz_agent import server_nasa
from max_postiz_agent.config import AgentError
def owned(module):
    assert Path(module.__file__).resolve().is_relative_to(wheel), 'Module came from editable source'
owned(server_nasa)
from max_postiz_agent import __version__
from importlib.metadata import distribution
assert __version__ == sys.argv[2], 'Installed wheel version differs'
assert Path(distribution('max-postiz-agent').locate_file('')).resolve() == wheel, 'Metadata came from editable source'
base = 'https://noirlab.edu/public/images/iotw2601a/'
legacy = server_nasa._parser(science_mailto=False)
science = server_nasa._parser(science_mailto=True)
again = server_nasa._parser(science_mailto=False)
for parser in (legacy, science, again):
    owned(parser)
assert len({id(legacy), id(science), id(again)}) == 3
for parser in (legacy, science, again):
    assert parser.narrative_text('<p>News <a href="https://example.org/ref?a=1&amp;b=2">paper</a>.</p>', base) == 'News [paper](https://example.org/ref?a=1&b=2).'
for address in ('mailto:comms@noirlab.edu', 'mailto:info@noirlab.edu'):
    assert science.narrative_text('<p>News <a href="' + address + '">team</a>.</p>', base) == 'News [team](' + address + ').'
    for parser in (legacy, again):
        try:
            parser._narrative_href(address, base)
        except AgentError as exc:
            assert exc.code == 'ARTICLE_SOURCE_LINK_INVALID'
        else:
            raise AssertionError('Science mailto override leaked to legacy parser')
try:
    science._narrative_href('mailto:outside@example.org', base)
except AgentError as exc:
    assert exc.code == 'ARTICLE_SOURCE_LINK_INVALID'
else:
    raise AssertionError('Foreign mailto accepted')
readback = Path(sys.argv[5]).resolve()
assert readback.is_relative_to(wheel)
sys.path.insert(0, str(readback.parent))
readback_module = runpy.run_path(str(readback))
readback_module['load_verified_application'](sys.argv[2])
for name, module in tuple(sys.modules.items()):
    if name == 'max_postiz_agent' or name.startswith('max_postiz_agent.'):
        owned(module)
assert not attempted, 'Forbidden capability was attempted and swallowed'
print(json.dumps({'contract': 'installed-wheel-extractor-probe-v1',
                  'passed': ['nasa_links_v1', 'science_mailto_isolation', 'readback_runtime_api'],
                  'server_nasa_origin': server_nasa.__file__,
                  'extractor_origin': legacy.__file__, 'network_or_process_calls': 0}))
"""


def interpreter(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def configuration(root: Path) -> dict:
    # Импорт после bootstrap: системный Python может быть старше Python в .venv.
    import tomllib

    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def test_environment(environ: dict[str, str], *, postgres: bool) -> dict[str, str]:
    """URL тестовой БД передаётся только явным флагом; .env не читается."""
    env = dict(environ)
    for key in ("DATABASE_URL", "PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    if not postgres:
        for key in (
            "TEST_DATABASE_URL",
            "MONITOR_OWNED_PG_TEST_URL",
            "MAX_POSTIZ_PG_TEST_CONTRACT",
        ):
            env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def contracts(root: Path) -> dict:
    """Проверяем checkout и locks, не импортируя приложение/его конфигурацию."""
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    project = configuration(root)
    if not SpecifierSet(project["project"]["requires-python"]).contains(
        ".".join(map(str, sys.version_info[:3]))
    ):
        raise ValueError("Python does not satisfy pyproject.toml")
    tree = ast.parse((root / "src/max_postiz_agent/__init__.py").read_text(encoding="utf-8"))
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    version = project["project"]["version"]
    if versions != [version] or importlib.metadata.version("max-postiz-agent") != version:
        raise ValueError("Package versions differ; reinstall with pip install --no-deps -e .")
    spec = importlib.util.find_spec("max_postiz_agent")
    if (
        not spec
        or not spec.origin
        or Path(spec.origin).resolve().parent != root / "src/max_postiz_agent"
    ):
        raise ValueError("Installed package points to another checkout")
    checked = set()
    for name in LOCKS:
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            if not re.fullmatch(r"==[^,*]+", str(requirement.specifier)):
                raise ValueError(f"Unpinned dependency in {name}")
            installed = importlib.metadata.version(requirement.name)
            if installed not in requirement.specifier:
                raise ValueError(f"Lock mismatch: {requirement.name} ({name})")
            checked.add(requirement.name.lower())
    if not any((root / "tests").rglob("test_*.py")):
        raise ValueError("No tests found in tests/")
    paths = project.get("tool", {}).get("max-postiz", {}).get("check", {}).get("format-paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(
            not isinstance(name, str)
            or not (root / name).resolve().is_relative_to(root)
            or not (root / name).is_file()
            for name in paths
        )
    ):
        raise ValueError("Missing or invalid tool.max-postiz.check.format-paths")
    for tool in ("git", "node"):
        if not shutil.which(tool):
            raise ValueError(f"Required tool missing: {tool}")
    return {"package_version": version, "locked_dependencies": len(checked)}


def postgres_ready(url: str | None) -> None:
    """Допускаем только явно указанную отдельную тестовую БД; URL не печатаем."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    if not url:
        raise ValueError("--postgres requires TEST_DATABASE_URL (a dedicated test database)")
    try:
        parsed = make_url(url)
    except Exception:
        raise ValueError("Invalid TEST_DATABASE_URL; value withheld") from None
    if (
        parsed.drivername != "postgresql+psycopg"
        or not re.fullmatch(r"(?:test_[a-z0-9_]+|[a-z0-9_]+_tests)", parsed.database or "")
        or parsed.query
    ):
        raise ValueError("Use postgresql+psycopg, a test_*/*_tests database and no URL query")
    engine = create_engine(parsed, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        raise ValueError("Test PostgreSQL unavailable; connection details withheld") from None
    finally:
        engine.dispose()


def junit_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()  # noqa: S314 -- наш локальный отчёт pytest
    cases = list(root.iter("testcase"))
    failed = [case for case in cases if case.find("failure") is not None]
    errors = [case for case in cases if case.find("error") is not None]
    skipped = [case for case in cases if case.find("skipped") is not None]
    return {
        "total": len(cases),
        "passed": len(cases) - len(failed) - len(errors) - len(skipped),
        "failed": len(failed),
        "errors": len(errors),
        "skipped": len(skipped),
        "skip_reasons": sorted({case.find("skipped").get("message", "") for case in skipped}),
        "failed_tests": [
            case.get("classname", "") + "::" + case.get("name", "") for case in failed + errors
        ],
    }


def owned_postgres_summary(path: Path) -> dict:
    """Явный новый PG-контракт не может стать зелёным через пропуски его тестов."""
    cases = list(ET.parse(path).getroot().iter("testcase"))  # noqa: S314 -- локальный pytest
    groups = {}
    for name, minimum, required_test in (
        ("test_route_dispatch_postgres", 1, None),
        (
            "test_route_recovery",
            1,
            "test_postgres_recovery_skips_locked_sender_then_reconciles_during_pause",
        ),
        ("test_monitor_configuration_postgres", 9, None),
        ("test_preapproval_retirement_postgres", 28, None),
        ("test_max_plaintext_postgres", 4, None),
        ("test_unbound_selection_reconciliation_postgres", 16, None),
    ):
        selected = [
            case
            for case in cases
            if case.get("classname", "").split(".")[-1] == name
            and (required_test is None or case.get("name") == required_test)
        ]
        passed = sum(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for case in selected
        )
        if len(selected) < minimum or passed != len(selected):
            raise ValueError(
                "Required isolated route/monitor/preapproval PostgreSQL cases missing or not passed"
            )
        groups[name] = {"total": len(selected), "passed": passed}
    return groups


def wheel_sources(root: Path) -> dict[str, bytes]:
    """Fresh build context includes tracked metadata/backend files and new package modules."""
    completed = subprocess.run(
        [shutil.which("git"), "ls-files", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        check=True,
        timeout=30,
    )
    names = set(completed.stdout.decode("utf-8").split("\0")) - {""}
    names.update(("scripts/readback_wheel_proof.py", "scripts/readback_max.py"))
    package_paths = list((root / "src/max_postiz_agent").rglob("*"))
    if any(path.is_symlink() for path in package_paths):
        raise ValueError("Symlink in installed-wheel package source")
    names.update(path.relative_to(root).as_posix() for path in package_paths if path.is_file())
    result = {}
    total = 0
    for name in sorted(names):
        relative = PurePosixPath(name)
        if (
            "__pycache__" in relative.parts
            or any(part.endswith(".egg-info") for part in relative.parts)
            or relative.suffix in {".pyc", ".pyo"}
        ):
            continue
        path = root / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file()
            or path.stat().st_size > 16 * 1024 * 1024
        ):
            raise ValueError("Unsafe or missing installed-wheel build source")
        raw = path.read_bytes()
        total += len(raw)
        if total > 128 * 1024 * 1024:
            raise ValueError("Installed-wheel build source exceeds bound")
        result[name] = raw
    return result


def installed_wheel(root: Path, directory: Path, xml: Path) -> int:
    """Build and probe an actual wheel; never import the editable application here."""
    suite = ET.Element("testsuite", name="installed_wheel", tests=str(len(WHEEL_CASES)))
    cases = {name: ET.SubElement(suite, "testcase", name=name) for name in WHEEL_CASES}
    outcome = {"contract": "installed-wheel-gate-v1", "status": "failed"}
    directory.mkdir(mode=0o700)
    try:
        source, dist, extracted = (directory / name for name in ("source", "dist", "site-packages"))
        source.mkdir()
        dist.mkdir()
        extracted.mkdir()
        before = wheel_sources(root)
        for name, raw in before.items():
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--wheel-dir",
                str(dist),
                str(source),
            ],
            cwd=directory,
            check=True,
            timeout=180,
        )
        wheels = list(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one built wheel")
        wheel = wheels[0]
        readback_members = {}
        with zipfile.ZipFile(wheel) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or sum(i.file_size for i in infos) > 64 * 1024 * 1024:
                raise ValueError("Invalid installed-wheel archive bound or duplicate member")
            for script in ("readback_max.py", "readback_wheel_proof.py"):
                suffix = ".data/data/share/max-postiz/" + script
                matches = [name for name in names if name.endswith(suffix)]
                if len(matches) != 1 or archive.read(matches[0]) != before["scripts/" + script]:
                    raise ValueError("Missing or changed readback script in wheel")
                readback_members[script] = matches[0]
            for info in infos:
                name = PurePosixPath(info.filename)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.file_size > 16 * 1024 * 1024
                ):
                    raise ValueError("Unsafe installed-wheel member")
                destination = extracted / name
                if not destination.resolve().is_relative_to(extracted.resolve()):
                    raise ValueError("Installed-wheel member escapes extraction directory")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(info))
        verifier = extracted / readback_members["readback_wheel_proof.py"]
        proof_process = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-I",
                "-B",
                "-c",
                WHEEL_PROOF_PROBE,
                str(extracted),
                str(wheel.resolve()),
                str(verifier.resolve()),
                str(root),
                str(source),
            ],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        (directory / "proof.stdout.log").write_bytes(proof_process.stdout)
        (directory / "proof.stderr.log").write_bytes(proof_process.stderr)
        if proof_process.returncode:
            print(proof_process.stderr.decode("utf-8", errors="backslashreplace"), flush=True)
            raise ValueError("Installed-wheel readback proof failed")
        proof = json.loads(proof_process.stdout)
        if proof != {"contract": "installed-readback-wheel-proof-v1", "passed": True}:
            raise ValueError("Invalid installed-wheel readback proof result")
        completed = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-I",
                "-B",
                "-c",
                WHEEL_PROBE,
                str(extracted),
                configuration(root)["project"]["version"],
                str(root),
                str(source),
                str(extracted / readback_members["readback_max.py"]),
            ],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        (directory / "probe.stdout.log").write_bytes(completed.stdout)
        (directory / "probe.stderr.log").write_bytes(completed.stderr)
        if completed.returncode:
            print(
                completed.stderr.decode("utf-8", errors="backslashreplace")
                .encode("ascii", errors="backslashreplace")
                .decode("ascii"),
                flush=True,
            )
            raise ValueError("Installed-wheel isolated probe failed")
        probe = json.loads(completed.stdout)
        if (
            probe.get("contract") != "installed-wheel-extractor-probe-v1"
            or probe.get("passed") != list(WHEEL_CASES[:-1])
            or probe.get("network_or_process_calls") != 0
        ):
            raise ValueError("Invalid installed-wheel probe result")
        if wheel_sources(root) != before:
            raise ValueError("Installed-wheel source changed during validation")
        outcome.update(
            status="passed",
            probe=probe,
            readback_proof=proof,
            wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            source_files=len(before),
            source_hash=hashlib.sha256(
                json.dumps(
                    {n: hashlib.sha256(raw).hexdigest() for n, raw in before.items()},
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        )
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        outcome["error"] = type(exc).__name__
        for case in cases.values():
            ET.SubElement(case, "error", message="Installed wheel build/probe did not pass")
        print("Installed wheel build/probe failed: " + type(exc).__name__, flush=True)
    with xml.open("xb") as stream:
        ET.ElementTree(suite).write(stream, encoding="utf-8", xml_declaration=True)
    with (directory / "report.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(outcome, indent=2))
    return 0 if outcome["status"] == "passed" else 1


def installed_wheel_summary(path: Path) -> dict:
    summary = junit_summary(path)
    cases = list(ET.parse(path).getroot().iter("testcase"))  # noqa: S314 -- наш wheel report
    if (
        sorted(case.get("name", "") for case in cases) != sorted(WHEEL_CASES)
        or summary["passed"] != len(WHEEL_CASES)
        or summary["skipped"]
        or summary["failed"]
        or summary["errors"]
    ):
        raise ValueError("Required installed-wheel cases missing or not passed")
    return summary


class Checks:
    def __init__(self, root: Path, directory: Path, env: dict[str, str]):
        self.root, self.directory, self.env = root, directory, env
        self.results: list[dict] = []

    def stage(self, name: str, command: list[str], *, timeout: int = 3600) -> dict:
        log = self.directory / (name + ".log")
        print(f"[RUN] {name}", flush=True)
        started = time.monotonic()
        result = {
            "name": name,
            "log": log.relative_to(self.root).as_posix(),
            "timeout_seconds": timeout,
        }
        try:
            with log.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(
                    command,
                    cwd=self.root,
                    env=self.env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
            result.update(
                status="passed" if completed.returncode == 0 else "failed",
                exit_code=completed.returncode,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.update(status="failed", exit_code=1, error=type(exc).__name__)
        result["seconds"] = round(time.monotonic() - started, 3)
        self.results.append(result)
        print(
            f"[{result['status'].upper()}] {name} ({result['seconds']}s) — {result['log']}",
            flush=True,
        )
        return result


def run(root: Path, *, postgres: bool, test_timeout: int = 7200) -> int:
    if type(test_timeout) is not int or not 7200 <= test_timeout <= 21600:
        raise ValueError("Full pytest timeout must be between 7200 and 21600 seconds")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / "data/check-runs" / (stamp + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    env = test_environment(dict(os.environ), postgres=postgres)
    report = {
        "schema_version": 1,
        "root": str(root),
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "postgres_requested": postgres,
        "started_at_utc": stamp,
        "live_delivery_checked": False,
    }
    checks = Checks(root, directory, env)
    python = [sys.executable, "-X", "utf8", "-B"]
    try:
        report["environment"] = contracts(root)
        if postgres:
            postgres_ready(env.get("TEST_DATABASE_URL"))
        print("[PASSED] environment (locks, package, tests, Git, Node)", flush=True)
    except (ValueError, OSError, ImportError, importlib.metadata.PackageNotFoundError) as exc:
        # Сообщения выше контролируются нами; значения окружения не включаем.
        report.update(status="failed", environment_error=str(exc), stages=[])
        (directory / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[FAILED] environment: {exc}\nReport: {directory / 'report.json'}", flush=True)
        return 1
    checks.stage("dependencies", python + ["-m", "pip", "check"])
    wheel_xml = directory / "installed-wheel.xml"
    wheel = checks.stage(
        "installed-wheel",
        python
        + [
            "scripts/check.py",
            "--installed-wheel",
            str(directory / "installed-wheel"),
            "--junitxml=" + str(wheel_xml),
        ],
        timeout=300,
    )
    try:
        report["installed_wheel_tests"] = installed_wheel_summary(wheel_xml)
    except (ET.ParseError, OSError, ValueError):
        wheel.update(
            status="failed", exit_code=1, error="Required installed-wheel report missing or invalid"
        )
    checks.stage("lint", python + ["-m", "ruff", "check", "src", "tests", "scripts"])
    paths = configuration(root)["tool"]["max-postiz"]["check"]["format-paths"]
    checks.stage("format", python + ["-m", "ruff", "format", "--check", *paths])
    for asset in ("static/dashboard.js", "hub_static/app.js"):
        checks.stage(
            "javascript-" + Path(asset).stem,
            [shutil.which("node"), "--check", "src/max_postiz_agent/" + asset],
        )
    owned_preflight = None
    if postgres and env.get("MAX_POSTIZ_PG_TEST_CONTRACT"):
        owned_preflight = checks.stage(
            "owned-postgres-preflight", python + ["scripts/check_ci_postgres.py"], timeout=60
        )
    browser = checks.stage(
        "browser",
        python
        + [
            "-c",
            (
                "from playwright.sync_api import sync_playwright; "
                "p=sync_playwright().start(); "
                "b=p.chromium.launch(channel='chrome', headless=True, chromium_sandbox=True); "
                "b.close(); p.stop(); print('Isolated Chrome ready; no external page opened')"
            ),
        ],
        timeout=60,
    )
    xml = directory / "tests.xml"
    if (
        wheel["status"] == "passed"
        and browser["status"] == "passed"
        and (owned_preflight is None or owned_preflight["status"] == "passed")
    ):
        temporary = directory / "pytest"
        tests = checks.stage(
            "tests",
            python
            + [
                "-m",
                "pytest",
                "-q",
                "--basetemp=" + str(temporary),
                "--junitxml=" + str(xml),
                "-o",
                "faulthandler_timeout=120",
            ],
            timeout=test_timeout,
        )
        if xml.is_file():
            try:
                summary = report["tests"] = junit_summary(xml)
                if postgres and env.get("MAX_POSTIZ_PG_TEST_CONTRACT"):
                    report["owned_postgres_tests"] = owned_postgres_summary(xml)
                if (
                    summary["failed"]
                    or summary["errors"]
                    or summary["passed"] == 0
                    or (
                        postgres
                        and any("TEST_DATABASE_URL" in reason for reason in summary["skip_reasons"])
                    )
                ):
                    tests.update(
                        status="failed",
                        exit_code=1,
                        error="Failed/empty test suite or PostgreSQL tests skipped",
                    )
                print("Tests: " + json.dumps(summary, ensure_ascii=False), flush=True)
            except (ET.ParseError, OSError, ValueError):
                tests.update(
                    status="failed",
                    exit_code=1,
                    error="Invalid pytest report or incomplete isolated PostgreSQL coverage",
                )
        elif tests["status"] == "passed":
            tests.update(status="failed", exit_code=1, error="Missing pytest report")
        if tests.get("error"):
            print("[FAILED] tests validation: " + tests["error"], flush=True)
    else:
        checks.results.append(
            {
                "name": "tests",
                "status": "blocked",
                "error": "Installed wheel, Chrome or owned PostgreSQL preflight failed",
            }
        )
    checks.stage("diff", [shutil.which("git"), "diff", "--check"])
    report.update(
        status="passed" if all(s["status"] == "passed" for s in checks.results) else "failed",
        stages=checks.results,
    )
    report["limitations"] = [
        "Type checking is not configured; formatter scope is explicit in pyproject.toml",
        "MAX/Postiz/model/LanguageTool live services are not exercised",
    ]
    if not postgres:
        report["limitations"].append(
            "PostgreSQL tests are skipped; run with --postgres for database acceptance"
        )
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{report['status'].upper()}] Report: {path}", flush=True)
    return 0 if report["status"] == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="Require dedicated TEST_DATABASE_URL and PostgreSQL tests",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=7200,
        help="Full pytest time limit in seconds (7200–21600, default: 7200)",
    )
    parser.add_argument("--installed-wheel", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--junitxml", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 7200 <= args.test_timeout <= 21600:
        parser.error("--test-timeout must be between 7200 and 21600 seconds")
    target = interpreter(ROOT)
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        if not target.is_file():
            print(
                "[FAILED] Missing .venv; create Python 3.12 venv and install the documented lock files"
            )
            return 1
        return subprocess.run(
            [
                str(target),
                "-X",
                "utf8",
                "-B",
                str(Path(__file__).resolve()),
                *(argv if argv is not None else sys.argv[1:]),
            ],
            cwd=ROOT,
            check=False,
        ).returncode
    if args.installed_wheel is not None:
        directory = args.installed_wheel.resolve()
        if (
            args.junitxml is None
            or not directory.is_relative_to((ROOT / "data/check-runs").resolve())
            or args.junitxml.resolve().parent != directory.parent
        ):
            parser.error("Installed wheel probe requires its own check-run directory/report")
        return installed_wheel(ROOT, directory, args.junitxml.resolve())
    if args.junitxml is not None:
        parser.error("--junitxml is internal to the installed-wheel stage")
    return run(ROOT, postgres=args.postgres, test_timeout=args.test_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
