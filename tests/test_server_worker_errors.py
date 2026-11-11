import json
import logging
from types import FunctionType, SimpleNamespace

import pytest

from max_postiz_agent import server_cli
from max_postiz_agent.config import AgentError


def step(n):
    _private_local = "LOCAL_PASSWORD_NOT_FOR_LOGS"
    if n:
        return step(n - 1)
    raise ValueError("https://user:password@private.invalid/RAW_BODY")


def failure(*, module="max_postiz_agent.article_cycle", depth=1):
    namespace = {"__name__": module}
    code = step.__code__.replace(co_filename="https://private.invalid/SECRET_SOURCE_PATH")
    namespace["step"] = FunctionType(code, namespace)
    try:
        namespace["step"](depth)
    except ValueError as exc:
        return exc
    raise AssertionError("exception not raised")


def report(caplog, exc, stage="controller"):
    with caplog.at_level(logging.ERROR, logger=server_cli.__name__):
        server_cli._log_unexpected_exception(exc, stage)
    records = [r for r in caplog.records if r.message.startswith("EDITORIAL_UNEXPECTED_EXCEPTION ")]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    return json.loads(records[0].message.split(" ", 1)[1])


def test_generic_failure_has_safe_durable_journal_fields(caplog):
    value = report(caplog, failure())
    assert value == {"exception_type": "ValueError", "stage": "controller", "frames": [
        {"module": "max_postiz_agent.article_cycle", "function": "step"},
        {"module": "max_postiz_agent.article_cycle", "function": "step"},
    ]}
    output = caplog.text
    for secret in ("password", "private.invalid", "RAW_BODY", "LOCAL_PASSWORD", "SECRET_SOURCE_PATH"):
        assert secret not in output


def test_exception_string_and_repr_are_never_called(caplog):
    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("str must not be called")

        def __repr__(self):
            raise AssertionError("repr must not be called")

    exc = UnprintableError("PRIVATE_ARGUMENT")
    assert report(caplog, exc)["exception_type"] == "UnprintableError"
    assert "PRIVATE_ARGUMENT" not in caplog.text


@pytest.mark.parametrize("module", ["sqlalchemy.engine", "other.max_postiz_agent", "max_postiz_agent.bad/secret"])
def test_only_application_module_identifiers_are_logged(caplog, module):
    assert report(caplog, failure(module=module))["frames"] == []


def test_frames_are_bounded_and_no_cause_is_logged(caplog):
    exc = failure(depth=100)
    exc.__cause__ = RuntimeError("CAUSE_PASSWORD")
    value = report(caplog, exc)
    assert len(value["frames"]) == 8
    assert "CAUSE_PASSWORD" not in caplog.text


def test_trace_walk_stops_before_distant_application_leaf(caplog):
    previous = None
    for index in range(101):
        namespace = {"__name__": "max_postiz_agent.article_cycle"}
        current = FunctionType(step.__code__.replace(co_name="step" + str(index)), namespace)
        namespace["step"] = previous or current
        previous = current
    try:
        previous(100)
    except ValueError as exc:
        value = report(caplog, exc)
    assert len(value["frames"]) == 8
    assert all(int(frame["function"][4:]) >= 30 for frame in value["frames"])


def test_unknown_stage_is_not_reflected(caplog):
    assert report(caplog, failure(), "https://SECRET.invalid")["stage"] == "unknown"
    assert "SECRET.invalid" not in caplog.text


def test_agent_error_keeps_existing_code_without_new_journal_noise(caplog):
    exc = AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
    with caplog.at_level(logging.ERROR, logger=server_cli.__name__):
        server_cli._log_unexpected_exception(exc, "controller")
    assert exc.code == "ARTICLE_RECONCILIATION_REQUIRED"
    assert not caplog.records


def test_broken_logging_never_replaces_original_worker_exception(monkeypatch):
    logger = SimpleNamespace(error=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("logging failed")))
    monkeypatch.setattr(server_cli, "logging", SimpleNamespace(getLogger=lambda name: logger))
    server_cli._log_unexpected_exception(failure(), "controller")


def test_generic_catches_log_before_unchanged_runtime_error_update():
    # Check the actual worker closures, including preparation: no fake exception
    # handler can accidentally satisfy this regression.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(server_cli))
    for name, code in (("worker", "EDITORIAL_WORKER_ERROR"), ("prepare_worker", "EDITORIAL_PREPARATION_ERROR")):
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        handler = next(n for n in ast.walk(function) if isinstance(n, ast.ExceptHandler) and n.name == "exc")
        first = handler.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
        assert isinstance(first.value.func, ast.Name) and first.value.func.id == "_log_unexpected_exception"
        assert code in ast.unparse(handler)
        assert "last_error={'code': code}" in ast.unparse(handler)


@pytest.mark.parametrize(("failed_stage", "journal_enabled", "tracked"), [
    ("dispatch_tick", False, False),
    ("platform_publish_due", False, False),
    ("controller", False, False),
    ("preparation", False, False),
    ("agent", False, False),
    ("schedule_recovery", True, False),
    ("schedule_recovery", False, True),
])
def test_actual_worker_loops_keep_error_codes_and_shutdown(
        monkeypatch, tmp_path, caplog, failed_stage, journal_enabled, tracked):
    import asyncio
    from contextlib import contextmanager
    import threading

    import uvicorn
    from max_postiz_agent import hourly_reserve, schedule_recovery

    observed = threading.Event()
    errors = []
    closed = []

    def maybe_fail(stage):
        if failed_stage == stage:
            raise ValueError("PRIVATE_PASSWORD")
        if failed_stage == "agent" and stage == "controller":
            raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)

    class Store:
        def read(self):
            return {}

        @contextmanager
        def worker_lock(self):
            yield True

        def phase(self, phase, **details):
            errors.append(details["last_error"])
            observed.set()

        def progress(self, channel, phase, **details):
            self.phase(phase, **details)

    core = SimpleNamespace(
        settings=SimpleNamespace(root=tmp_path),
        dispatch_tick=lambda: maybe_fail("dispatch_tick"),
        publish_due=lambda **kw: maybe_fail("platform_publish_due"),
        close=lambda: closed.append(True),
    )
    if journal_enabled:
        core.postiz = SimpleNamespace(schedule_journal_enabled=True)
    application = SimpleNamespace(router=SimpleNamespace(), state=SimpleNamespace())

    def fake_cycle(*args, **kwargs):
        return SimpleNamespace(run_once=lambda: maybe_fail("controller"),
                               prepare_once=lambda: maybe_fail("preparation"))

    def serve(app, **kwargs):
        async def lifespan():
            async with app.router.lifespan_context(app):
                assert await asyncio.to_thread(observed.wait, 2)
        asyncio.run(lifespan())

    monkeypatch.setattr(server_cli, "build", lambda *a, **kw: core)
    monkeypatch.setattr(server_cli, "ServerState", lambda core: Store())
    monkeypatch.setattr(server_cli, "HourlyArticleCycle", lambda core: object())
    ledger = {"records": [{"schedule_journal": {"operation": "fixture"}}]} if tracked else {}
    monkeypatch.setattr(server_cli, "DatabaseArticleLedger", lambda cycle: SimpleNamespace(load=lambda: ledger))
    if failed_stage == "schedule_recovery":
        monkeypatch.setattr(schedule_recovery, "ScheduleRecovery",
                            lambda core: SimpleNamespace(run_once=lambda: maybe_fail("schedule_recovery")))
    monkeypatch.setattr(server_cli, "editorial_app", lambda *a: application)
    monkeypatch.setattr(hourly_reserve, "ReserveServerCycle", fake_cycle)
    monkeypatch.setattr(uvicorn, "run", serve)
    with caplog.at_level(logging.ERROR, logger=server_cli.__name__):
        server_cli.main(["--root", str(tmp_path), "serve"])
    assert closed == [True]
    expected = ("ARTICLE_RECONCILIATION_REQUIRED" if failed_stage == "agent"
                else "EDITORIAL_PREPARATION_ERROR" if failed_stage == "preparation"
                else "EDITORIAL_WORKER_ERROR")
    assert errors == [{"code": expected}]
    logged = [r for r in caplog.records if r.message.startswith("EDITORIAL_UNEXPECTED_EXCEPTION ")]
    if failed_stage == "agent":
        assert logged == []
    else:
        assert len(logged) == 1
        assert json.loads(logged[0].message.split(" ", 1)[1])["stage"] == failed_stage
        assert "PRIVATE_PASSWORD" not in caplog.text
