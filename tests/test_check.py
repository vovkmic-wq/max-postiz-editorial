"""Проверяем код возврата и изоляцию проверки, не запускаем полную suite рекурсивно."""

import importlib.util
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "project_check", Path(__file__).resolve().parents[1] / "scripts/check.py"
)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def write_stage_junit(command, body=None):
    path = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
    if path is None:
        return
    if "--installed-wheel" in command:
        body = (
            "<testsuite>"
            + "".join(f'<testcase name="{n}"/>' for n in check.WHEEL_CASES)
            + "</testsuite>"
        )
    if body is not None:
        Path(path).write_text(body)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / "src/max_postiz_agent").mkdir(parents=True)
    (tmp_path / "src/max_postiz_agent/__init__.py").write_text('__version__ = "1.2.3"')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("def test_sample(): pass\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check.py").touch()
    (tmp_path / "scripts/readback_wheel_proof.py").write_text("# test verifier")
    (tmp_path / "scripts/readback_max.py").write_text("# test readback")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion="1.2.3"\nrequires-python=">=3.12"\n'
        '[tool.max-postiz.check]\nformat-paths=["scripts/check.py"]\n'
    )
    for name in check.LOCKS:
        (tmp_path / name).write_text("sample==1.0\n")
    monkeypatch.setattr(
        check.importlib.metadata,
        "version",
        lambda name: "1.2.3" if name == "max-postiz-agent" else "1.0",
    )
    monkeypatch.setattr(
        check.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(tmp_path / "src/max_postiz_agent/__init__.py")),
    )
    monkeypatch.setattr(check.shutil, "which", lambda name: "/fake/" + name)
    return tmp_path


def test_local_mode_does_not_forward_runtime_or_test_database_or_pytest_overrides():
    original = {
        "PATH": "tools",
        "DATABASE_URL": "runtime-private",
        "TEST_DATABASE_URL": "test-private",
        "MONITOR_OWNED_PG_TEST_URL": "owned-test-private",
        "MAX_POSTIZ_PG_TEST_CONTRACT": "monitor-main-integration-v1",
        "PYTHONPATH": "other-checkout",
        "PYTEST_ADDOPTS": "--collect-only",
        "PYTEST_PLUGINS": "injected",
    }
    result = check.test_environment(original, postgres=False)
    assert result["PATH"] == "tools" and result["PYTHONUTF8"] == "1"
    assert result["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert not set(original).difference({"PATH"}).intersection(result)
    assert original["DATABASE_URL"] == "runtime-private"


def test_postgres_mode_forwards_only_explicit_test_url():
    result = check.test_environment(
        {"DATABASE_URL": "runtime", "TEST_DATABASE_URL": "tests"}, postgres=True
    )
    assert result["TEST_DATABASE_URL"] == "tests" and "DATABASE_URL" not in result


def test_contracts_verify_current_checkout_and_all_locks(checkout):
    assert check.contracts(checkout) == {"package_version": "1.2.3", "locked_dependencies": 1}


@pytest.mark.parametrize(
    "damaged_group,damage",
    [(None, None)]
    + [
        (group, damage)
        for group in (
            "test_route_dispatch_postgres",
            "test_route_recovery",
            "test_monitor_configuration_postgres",
            "test_preapproval_retirement_postgres",
            "test_max_plaintext_postgres",
            "test_unbound_selection_reconciliation_postgres",
        )
        for damage in ("missing", "skipped", "failure", "error")
    ],
)
def test_owned_postgres_report_requires_all_six_suites_without_skips(
    tmp_path, damaged_group, damage
):
    groups = (
        ("test_route_dispatch_postgres", 1),
        ("test_route_recovery", 1),
        ("test_monitor_configuration_postgres", 9),
        ("test_preapproval_retirement_postgres", 28),
        ("test_max_plaintext_postgres", 4),
        ("test_unbound_selection_reconciliation_postgres", 16),
    )
    cases = []
    for group, count in groups:
        for index in range(count):
            damaged = group == damaged_group and index == 0
            if damaged and damage == "missing":
                continue
            marker = f"<{damage}/>" if damaged else ""
            case_name = (
                "test_postgres_recovery_skips_locked_sender_then_reconciles_during_pause"
                if group == "test_route_recovery"
                else f"case_{index}"
            )
            cases.append(
                f'<testcase classname="tests.{group}" name="{case_name}">{marker}</testcase>'
            )
    path = tmp_path / "junit.xml"
    path.write_text("<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>")
    if damage is None:
        assert check.owned_postgres_summary(path) == {
            "test_route_dispatch_postgres": {"total": 1, "passed": 1},
            "test_route_recovery": {"total": 1, "passed": 1},
            "test_monitor_configuration_postgres": {"total": 9, "passed": 9},
            "test_preapproval_retirement_postgres": {"total": 28, "passed": 28},
            "test_max_plaintext_postgres": {"total": 4, "passed": 4},
            "test_unbound_selection_reconciliation_postgres": {"total": 16, "passed": 16},
        }
    else:
        with pytest.raises(ValueError, match="PostgreSQL cases missing or not passed"):
            check.owned_postgres_summary(path)


def test_sqlite_recovery_cannot_replace_the_required_postgres_recovery_case(tmp_path):
    cases = [
        '<testcase classname="tests.test_route_dispatch_postgres" name="claim"/>',
        '<testcase classname="tests.test_route_recovery" '
        'name="test_worker_expires_owned_lease_once_and_fences_original_callback"/>',
    ]
    cases.extend(
        f'<testcase classname="tests.test_monitor_configuration_postgres" name="fence_{index}"/>'
        for index in range(9)
    )
    path = tmp_path / "junit.xml"
    path.write_text("<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>")
    with pytest.raises(ValueError, match="PostgreSQL cases missing or not passed"):
        check.owned_postgres_summary(path)


def test_owned_postgres_identity_failure_blocks_all_pytest_before_database_mutations(
    checkout, monkeypatch
):
    monkeypatch.setenv("MAX_POSTIZ_PG_TEST_CONTRACT", "monitor-main-integration-v1")
    monkeypatch.setenv("TEST_DATABASE_URL", "explicit-test-database")
    monkeypatch.setattr(check, "postgres_ready", lambda url: None)
    executed = []

    def execute(command, **kwargs):
        stage = Path(kwargs["stdout"].name).stem
        executed.append(stage)
        assert "pytest" not in command
        write_stage_junit(command)
        return SimpleNamespace(returncode=int(stage == "owned-postgres-preflight"))

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=True) == 1
    assert "owned-postgres-preflight" in executed and "tests" not in executed
    report = json.loads(next((checkout / "data/check-runs").glob("*/report.json")).read_text())
    assert report["status"] == "failed"
    assert (
        next(stage for stage in report["stages"] if stage["name"] == "tests")["status"] == "blocked"
    )


@pytest.mark.parametrize(
    "damage", ["version", "lock", "no_tests", "wrong_checkout", "missing_tool"]
)
def test_contracts_fail_instead_of_silently_skipping_prerequisites(checkout, monkeypatch, damage):
    if damage == "version":
        (checkout / "src/max_postiz_agent/__init__.py").write_text('__version__ = "0.0.1"')
    elif damage == "lock":
        (checkout / check.LOCKS[0]).write_text("sample==2.0\n")
    elif damage == "no_tests":
        (checkout / "tests/test_sample.py").unlink()
    elif damage == "wrong_checkout":
        monkeypatch.setattr(
            check.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=__file__)
        )
    else:
        monkeypatch.setattr(check.shutil, "which", lambda name: None)
    with pytest.raises(ValueError):
        check.contracts(checkout)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "invalid",
        "sqlite:///private-secret",
        "postgresql+psycopg://user:private-secret@localhost/runtime",
        "postgresql+psycopg://user:private-secret@localhost/app_tests?options=-csearch_path=public",
    ],
)
def test_postgres_preflight_rejects_runtime_or_ambiguous_urls_without_exposing_them(url):
    with pytest.raises(ValueError) as failed:
        check.postgres_ready(url)
    assert "private-secret" not in str(failed.value)


def test_stage_turns_timeout_into_failed_result(checkout, monkeypatch):
    def timeout(*args, **kwargs):
        raise check.subprocess.TimeoutExpired("private-args", 1)

    monkeypatch.setattr(check.subprocess, "run", timeout)
    directory = checkout / "data/run"
    directory.mkdir(parents=True)
    checks = check.Checks(checkout, directory, {})
    result = checks.stage("timeout", ["tool"])
    assert result["status"] == "failed" and result["exit_code"] != 0
    assert result["error"] == "TimeoutExpired" and "private-args" not in json.dumps(result)


@pytest.mark.parametrize(
    "case, expected",
    [
        ('<testcase name="ok"/>', 0),
        ('<testcase name="bad"><failure>failure</failure></testcase>', 1),
        ("", 1),
    ],
)
def test_unified_check_exit_code_reflects_real_test_result(checkout, monkeypatch, case, expected):
    def execute(command, **kwargs):
        xml = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        if xml:
            write_stage_junit(
                command, "<testsuites><testsuite>" + case + "</testsuite></testsuites>"
            )
            return SimpleNamespace(
                returncode=int("failure" in case and "--installed-wheel" not in command)
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == expected
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == ("passed" if expected == 0 else "failed")
    assert report["live_delivery_checked"] is False


@pytest.mark.parametrize(
    "suite_seconds, budget, explicit, expected",
    [
        (3601, 7200, False, 0),
        (7201, 7200, False, 1),
        (7201, 14400, True, 0),
        (14401, 14400, True, 1),
    ],
    ids=["default_success", "default_timeout", "extended_success", "extended_timeout"],
)
def test_full_suite_budget_preserves_long_success_and_bounded_timeout(
    checkout, monkeypatch, suite_seconds, budget, explicit, expected
):
    clock = SimpleNamespace(now=0.0)
    executed = {}
    monkeypatch.setattr(check.time, "monotonic", lambda: clock.now)

    def execute(command, **kwargs):
        stage = Path(kwargs["stdout"].name).stem
        executed[stage] = kwargs["timeout"]
        xml = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        duration = suite_seconds if "pytest" in command else 0.1
        clock.now += min(duration, kwargs["timeout"])
        if duration > kwargs["timeout"]:
            raise check.subprocess.TimeoutExpired(command, kwargs["timeout"])
        if xml:
            write_stage_junit(
                command,
                '<testsuites><testsuite><testcase name="virtual_complete_suite" '
                f'time="{suite_seconds}"/></testsuite></testsuites>',
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    options = {"test_timeout": budget} if explicit else {}
    assert check.run(checkout, postgres=False, **options) == expected
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == ("passed" if expected == 0 else "failed")
    assert list(executed) == [
        "dependencies",
        "installed-wheel",
        "lint",
        "format",
        "javascript-dashboard",
        "javascript-app",
        "browser",
        "tests",
        "diff",
    ]
    stages = {stage["name"]: stage for stage in report["stages"]}
    assert all(stage["timeout_seconds"] == executed[name] for name, stage in stages.items())
    assert (
        executed["tests"] == budget
        and executed["browser"] == 60
        and executed["installed-wheel"] == 300
    )
    assert all(
        budget == 3600
        for name, budget in executed.items()
        if name not in {"tests", "browser", "installed-wheel"}
    )
    assert all(stage["status"] == "passed" for name, stage in stages.items() if name != "tests")
    assert stages["tests"]["seconds"] == min(suite_seconds, budget)
    if expected == 0:
        assert stages["tests"]["status"] == "passed" and report["tests"]["passed"] == 1
    else:
        assert stages["tests"]["status"] == "failed" and stages["tests"]["exit_code"] != 0
        assert stages["tests"]["error"] == "TimeoutExpired"
        assert "tests" not in report


def test_lint_failure_does_not_hide_other_checks(checkout, monkeypatch):
    executed = []

    def execute(command, **kwargs):
        executed.append(command)
        xml = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        if xml:
            write_stage_junit(
                command, '<testsuites><testsuite><testcase name="ok"/></testsuite></testsuites>'
            )
        failed = "ruff" in command and "check" in command
        return SimpleNamespace(returncode=int(failed))

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    assert any("pytest" in command for command in executed)
    assert any("diff" in command for command in executed)


def test_environment_failure_still_writes_report_and_launches_nothing(checkout, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No stage may run with an invalid environment")

    monkeypatch.setattr(check.subprocess, "run", forbidden)
    (checkout / "tests/test_sample.py").unlink()
    assert check.run(checkout, postgres=False) == 1
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == "failed" and "No tests" in report["environment_error"]


@pytest.mark.parametrize("fixture_name", ["pg_hub", "postgres_reserve"])
def test_postgres_fixtures_never_read_runtime_configuration(tmp_path, monkeypatch, fixture_name):
    import test_hourly_reserve_controller
    import test_hub_postgres

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    local = tmp_path / "data/reserve-20261026/test-db.local.env"
    local.parent.mkdir(parents=True)
    value = "TEST_DATABASE_URL=postgresql+psycopg://private@localhost/runtime\n"
    local.write_text(value)
    (tmp_path / ".env.local").write_text(value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit runtime database must never be contacted")

    if fixture_name == "pg_hub":
        module = test_hub_postgres
        monkeypatch.setattr(module, "ROOT", tmp_path, raising=False)
        args = [tmp_path]
    else:
        module = test_hourly_reserve_controller
        monkeypatch.setattr(module, "__file__", str(tmp_path / "tests/test_controller.py"))
        args = [SimpleNamespace(), monkeypatch]
    monkeypatch.setattr(module, "create_engine", forbidden)
    generator = getattr(module, fixture_name).__wrapped__(*args)
    with pytest.raises(pytest.skip.Exception, match="TEST_DATABASE_URL"):
        next(generator)


@pytest.mark.parametrize(
    "xml",
    [
        "broken XML",
        '<testsuites><testsuite><testcase name="ok"/><testcase name="bad"><error/></testcase></testsuite></testsuites>',
    ],
)
def test_invalid_junit_or_false_success_exit_code_cannot_make_report_green(
    checkout, monkeypatch, xml
):
    def execute(command, **kwargs):
        path = next(
            (arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None
        )
        if path:
            write_stage_junit(command, xml)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert (
        next(stage for stage in report["stages"] if stage["name"] == "tests")["status"] == "failed"
    )


def test_launcher_uses_this_checkouts_venv_and_preserves_failure_code(checkout, monkeypatch):
    target = check.interpreter(checkout)
    target.parent.mkdir(parents=True)
    target.touch()
    monkeypatch.setattr(check, "ROOT", checkout)
    monkeypatch.setattr(check.sys, "prefix", str(checkout / "different-environment"))
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.main(["--postgres", "--test-timeout", "14400"]) == 7
    assert calls[0][0][0] == str(target)
    assert calls[0][0][-3:] == ["--postgres", "--test-timeout", "14400"]
    assert calls[0][1]["cwd"] == checkout


@pytest.mark.parametrize("value", [None, "7200", "14400", "21600"])
def test_cli_passes_full_suite_budget_to_run(checkout, monkeypatch, value):
    monkeypatch.setattr(check, "ROOT", checkout)
    monkeypatch.setattr(check.sys, "prefix", str(checkout / ".venv"))
    calls = []
    monkeypatch.setattr(check, "run", lambda root, **kwargs: calls.append((root, kwargs)) or 0)
    args = ["--postgres"] + (["--test-timeout", value] if value is not None else [])
    assert check.main(args) == 0
    assert calls == [(checkout, {"postgres": True, "test_timeout": int(value or "7200")})]


@pytest.mark.parametrize("value", ["7199", "21601", "-1", "0", "abc", "7200.5"])
def test_cli_rejects_invalid_test_budgets_before_starting_work(monkeypatch, value):
    monkeypatch.setattr(check, "run", lambda *args, **kwargs: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as error:
        check.main(["--test-timeout", value])
    assert error.value.code == 2


@pytest.mark.parametrize("value", [7199, 21601, True, "14400"])
def test_run_rejects_invalid_test_budgets_before_creating_report(checkout, value):
    with pytest.raises(ValueError, match="between 7200 and 21600"):
        check.run(checkout, postgres=False, test_timeout=value)
    assert not (checkout / "data/check-runs").exists()


def test_browser_failure_blocks_tests_but_still_reports_other_stages(checkout, monkeypatch):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        write_stage_junit(command)
        browser = any("chromium.launch" in arg for arg in command)
        return SimpleNamespace(returncode=int(browser))

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    assert not any("pytest" in command for command in calls)
    assert any("diff" in command for command in calls)
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert (
        next(stage for stage in report["stages"] if stage["name"] == "tests")["status"] == "blocked"
    )


def test_installed_wheel_failure_blocks_editable_suite(checkout, monkeypatch):
    executed = []

    def execute(command, **kwargs):
        stage = Path(kwargs["stdout"].name).stem
        executed.append(stage)
        path = next(
            (arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None
        )
        if path:
            Path(path).write_text('<testsuite><testcase name="editable_pass"/></testsuite>')
        return SimpleNamespace(returncode=int(stage == "installed-wheel"))

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    assert "installed-wheel" in executed and "tests" not in executed
    report = json.loads(next((checkout / "data/check-runs").glob("*/report.json")).read_text())
    assert report["status"] == "failed"
    assert next(s for s in report["stages"] if s["name"] == "tests")["status"] == "blocked"


@pytest.mark.parametrize(
    "damage", [None, "missing", "skipped", "failure", "error", "duplicate", "wrong_name"]
)
def test_installed_wheel_report_requires_all_semantic_cases(tmp_path, damage):
    names = list(check.WHEEL_CASES)
    if damage == "missing":
        names.pop()
    if damage == "duplicate":
        names.append(names[0])
    if damage == "wrong_name":
        names[-1] = "editable_source_test"
    cases = [
        f'<testcase name="{name}">{("<" + damage + "/>") if damage in {"skipped", "failure", "error"} and index == 0 else ""}</testcase>'
        for index, name in enumerate(names)
    ]
    path = tmp_path / "wheel.xml"
    path.write_text("<testsuite>" + "".join(cases) + "</testsuite>")
    if damage is None:
        assert check.installed_wheel_summary(path)["passed"] == len(check.WHEEL_CASES)
    else:
        with pytest.raises(ValueError, match="installed-wheel cases"):
            check.installed_wheel_summary(path)


@pytest.mark.parametrize("damage", ["missing", "wrong_names", "skipped"])
def test_false_success_wheel_command_cannot_hide_invalid_junit(checkout, monkeypatch, damage):
    def execute(command, **kwargs):
        stage = Path(kwargs["stdout"].name).stem
        assert stage != "tests"
        if stage == "installed-wheel" and damage != "missing":
            path = next(a.split("=", 1)[1] for a in command if a.startswith("--junitxml="))
            names = ["editable_fake"] if damage == "wrong_names" else list(check.WHEEL_CASES)
            Path(path).write_text(
                "<testsuite>"
                + "".join(
                    f'<testcase name="{n}">{"<skipped/>" if damage == "skipped" else ""}</testcase>'
                    for n in names
                )
                + "</testsuite>"
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    report = json.loads(next((checkout / "data/check-runs").glob("*/report.json")).read_text())
    assert next(s for s in report["stages"] if s["name"] == "installed-wheel")["status"] == "failed"


def test_wheel_build_context_preserves_declared_metadata_and_new_module_not_caches(
    checkout, monkeypatch
):
    (checkout / "README.md").write_text("Declared project readme")
    (checkout / "LICENSE").write_text("Project license")
    package = checkout / "src/max_postiz_agent"
    (package / "nasa_extractor.py").write_text("# New untracked package source")
    (package / "__pycache__").mkdir()
    (package / "__pycache__/stale.pyc").write_bytes(b"stale")
    (package / "__pycache__/stale.py").write_text("stale source")
    (checkout / ".env.local").write_text("PRIVATE_RUNTIME_NEVER_COPY")
    tracked = b"pyproject.toml\0README.md\0LICENSE\0src/max_postiz_agent/__init__.py\0"
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=tracked))
    sources = check.wheel_sources(checkout)
    assert set(sources) == {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "scripts/readback_wheel_proof.py",
        "scripts/readback_max.py",
        "src/max_postiz_agent/__init__.py",
        "src/max_postiz_agent/nasa_extractor.py",
    }
    assert sources["README.md"] == b"Declared project readme"


def test_wheel_build_context_refuses_traversal_instead_of_copying_private_files(
    checkout, monkeypatch
):
    monkeypatch.setattr(
        check.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=b"../private\0")
    )
    with pytest.raises(ValueError, match="Unsafe or missing"):
        check.wheel_sources(checkout)


@pytest.mark.parametrize("version", ["0.15.6", "99.17.42"])
def test_readback_wheel_gate_verifies_multiple_versions_offline(tmp_path, version):
    site = tmp_path / "site"
    site.mkdir()
    wheel = tmp_path / f"max_postiz_agent-{version}-py3-none-any.whl"
    files = {
        "max_postiz_agent/__init__.py": f'__version__ = "{version}"\n',
        "max_postiz_agent/browser_worker.py": "# package member\n",
        f"max_postiz_agent-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: max-postiz-agent\nVersion: {version}\n\n"
        ),
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode())
    root = Path(__file__).resolve().parents[1]
    verifier = tmp_path / "readback_wheel_proof.py"
    shutil.copyfile(root / "scripts/readback_wheel_proof.py", verifier)
    shutil.copyfile(root / "scripts/readback_max.py", tmp_path / "readback_max.py")
    result = check.subprocess.run(
        [
            check.sys.executable,
            "-I",
            "-B",
            "-c",
            check.WHEEL_PROOF_PROBE,
            str(site),
            str(wheel),
            str(verifier),
            str(root),
            str(tmp_path / "build-source"),
        ],
        cwd=tmp_path,
        stdout=check.subprocess.PIPE,
        stderr=check.subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout)["passed"] is True


@pytest.mark.parametrize(
    "attempt",
    [
        "socket.getaddrinfo('example.invalid', 443)",
        "subprocess.Popen(['must-not-run'])",
        "__import__('sys').audit('os.posix_spawn', 'never', [], {})",
        "__import__('sys').audit('os.exec', 'never', [], {})",
        "__import__('sys').audit('socket.sendmsg', None, [], [], None)",
        "__import__('sys').audit('os.fork')",
    ],
)
def test_isolated_wheel_probe_blocks_capabilities_even_during_import(tmp_path, attempt):
    package = tmp_path / "max_postiz_agent"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "server_nasa.py").write_text("import socket, subprocess\n" + attempt)
    result = check.subprocess.run(
        [
            check.sys.executable,
            "-X",
            "utf8",
            "-I",
            "-B",
            "-c",
            check.WHEEL_PROBE,
            str(tmp_path),
            "1.0",
            str(Path(__file__).resolve().parents[1]),
            str(tmp_path / "build-source"),
        ],
        cwd=tmp_path,
        stdout=check.subprocess.PIPE,
        stderr=check.subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert b"cannot access network or start processes" in result.stderr


def test_isolated_wheel_probe_cannot_pass_with_editable_package(tmp_path):
    result = check.subprocess.run(
        [
            check.sys.executable,
            "-X",
            "utf8",
            "-I",
            "-B",
            "-c",
            check.WHEEL_PROBE,
            str(tmp_path),
            "1.0",
            str(Path(__file__).resolve().parents[1]),
            str(tmp_path / "build-source"),
        ],
        cwd=tmp_path,
        stdout=check.subprocess.PIPE,
        stderr=check.subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0 and b"cannot read repository/build source" in result.stderr


def test_wheel_probe_rejects_repository_fallback_even_inside_packaged_module(tmp_path):
    package = tmp_path / "max_postiz_agent"
    package.mkdir()
    (package / "__init__.py").touch()
    root = Path(__file__).resolve().parents[1]
    source = f"from pathlib import Path\nPath({str(root / 'scripts/fetch_nasa_article.py')!r}).read_text()\n"
    (package / "server_nasa.py").write_text(source, encoding="utf-8")
    result = check.subprocess.run(
        [
            check.sys.executable,
            "-X",
            "utf8",
            "-I",
            "-B",
            "-c",
            check.WHEEL_PROBE,
            str(tmp_path),
            "1.0",
            str(root),
            str(tmp_path / "build-source"),
        ],
        cwd=tmp_path,
        stdout=check.subprocess.PIPE,
        stderr=check.subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0 and b"cannot read repository/build source" in result.stderr


def test_wheel_probe_allows_local_hostname_audit_for_windows_platform_import(tmp_path):
    package = tmp_path / "max_postiz_agent"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "server_nasa.py").write_text("import sys\nsys.audit('socket.gethostname')\n")
    root = Path(__file__).resolve().parents[1]
    result = check.subprocess.run(
        [
            check.sys.executable,
            "-X",
            "utf8",
            "-I",
            "-B",
            "-c",
            check.WHEEL_PROBE,
            str(tmp_path),
            "1.0",
            str(root),
            str(tmp_path / "build-source"),
        ],
        cwd=tmp_path,
        stdout=check.subprocess.PIPE,
        stderr=check.subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0 and b"max_postiz_agent.config" in result.stderr
    assert b"cannot access network" not in result.stderr
