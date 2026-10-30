"""Controller проверяется без Linux-процессов, моделей, HTTP и БД."""

import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def lab(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "deployment/translation"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("article_lab_under_test", directory / "run_article_lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path, lab):
    data = tmp_path / "data"
    data.mkdir()
    articles, routes = [], []
    for number in range(20):
        original = f"Complete scientific observation {number}: 2.5 km."
        articles.append({"id": f"A{number:02}", "source_id": "test", "title": "Full title", "original": original,
                         "url": f"https://science.example/{number}", "text_hash": lab.bench.text_hash(original),
                         "word_count": len(original.split()), "published_at": 1700000000})
    aliases = {"hy_current": lab.bench.HY_MODEL, "hy_glossary": lab.bench.HY_MODEL,
               "translategemma": "translategemma-12b-q4", "madlad400_3b": "madlad400-3b-q4k"}
    configs = {}
    for route in lab.ORDER:
        prompt = data / (route + ".txt")
        prompt.write_text("Frozen prompt " + route)
        routes.append({"id": route, "model": aliases[route], "base_url": f"http://127.0.0.1:{lab.PORTS[route]}",
                       "profile": {"backend_identity": "same" if route.startswith("hy_") else route,
                                   "prompt_sha256": lab.bench.sha(prompt.read_bytes()), "sampling": {"temperature": 0},
                                   "gateway_numeric_restore": False, "cache_policy": "retained",
                                   **({"glossary_sha256": "a" * 64} if route == "hy_glossary" else {})}})
        if route == "madlad400_3b":
            continue
        value = {"prompt_path": str(prompt), "gguf": {aliases[route]: "http://10.20.30.143:8089" if route.startswith("hy_") else "http://127.0.0.1:18087"}}
        if route.startswith("hy_"):
            value["memory_guard"] = {"model": lab.bench.HY_MODEL, "peer_props_url": lab.PROPS[0], "own_props_url": lab.PROPS[1]}
        config = data / (route + ".json")
        config.write_text(json.dumps(value))
        configs[route] = str(config)
    (data / "corpus.json").write_text(json.dumps({"schema_version": "translation-comparison-corpus-v1", "articles": articles}))
    (data / "routes.json").write_text(json.dumps({"schema_version": "translation-comparison-routes-v1", "routes": routes}))
    stub = data / "stub"
    stub.write_text("fixture")
    config = {"schema_version": "translation-article-lab-v1", "root": str(tmp_path), "state_dir": "data/controller",
              "corpus": "data/corpus.json", "routes": "data/routes.json", "output": "data/results.jsonl",
              **{key: str(stub) for key in ("python", "service", "native_gateway", "llama_server", "gemma_model", "gemma_template")},
              "gateway_configs": configs, "native": {key: str(stub) for key in ("worker", "model", "tokenizer", "config")},
              "resources": {"min_available_mb": {route: 9000 for route in lab.ORDER}, "poll_seconds": 1, "wait_timeout_seconds": 5}}
    path = data / "lab.json"
    path.write_text(json.dumps(config))
    return path


def test_dry_run_has_no_process_or_http(settings, lab, monkeypatch, capsys):
    monkeypatch.setattr(lab.subprocess, "Popen", lambda *_a, **_k: pytest.fail("process"))
    monkeypatch.setattr(lab.httpx, "Client", lambda *_a, **_k: pytest.fail("HTTP"))
    lab.main(["--config", str(settings)])
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_venv_python_symlink_is_preserved_for_every_child(settings, lab):
    config = json.loads(settings.read_text())
    managed = settings.parent / "managed-python"
    managed.write_text("managed executable fixture")
    python = settings.parent / "venv/bin/python"
    python.parent.mkdir(parents=True)
    try:
        python.symlink_to(managed)
    except OSError as exc:
        pytest.skip("Symlink privilege unavailable on this host: " + str(exc))
    config["python"] = str(python)
    settings.write_text(json.dumps(config))
    loaded, _ = lab.load_config(settings)
    assert python.resolve() == managed and loaded["python"] == python.absolute()
    assert loaded["python"] != managed
    for route in lab.ORDER:
        gateway = lab.commands(loaded, route, settings.parent / "ready.json")[-1]
        assert gateway[0] == str(python.absolute())


@pytest.mark.parametrize("damage", ["endpoint", "backend", "prompt", "guard", "alias", "private"])
def test_frozen_contract_damage_blocks(settings, lab, damage):
    config = json.loads(settings.read_text())
    if damage == "private":
        config["output"] = str(settings.parent.parent / "public.jsonl")
        settings.write_text(json.dumps(config))
    elif damage in {"endpoint", "alias"}:
        routes = json.loads((settings.parent / "routes.json").read_text())
        target = next(route for route in routes["routes"] if route["id"] == "translategemma")
        target["base_url" if damage == "endpoint" else "model"] = "http://10.20.30.143:4007" if damage == "endpoint" else "wrong-model"
        (settings.parent / "routes.json").write_text(json.dumps(routes))
    elif damage == "prompt":
        (settings.parent / "hy_current.txt").write_text("changed")
    else:
        path = Path(config["gateway_configs"]["hy_current"])
        value = json.loads(path.read_text())
        if damage == "backend":
            value["gguf"][lab.bench.HY_MODEL] = "http://127.0.0.1:8080"
        else:
            value["memory_guard"]["own_props_url"] = lab.PROPS[0]
        path.write_text(json.dumps(value))
    with pytest.raises(lab.bench.AgentError):
        lab.load_config(settings)


def test_commands_only_owned_loopback_and_exact_model_flags(settings, lab):
    config, _ = lab.load_config(settings)
    all_commands = [command for route in lab.ORDER for command in lab.commands(config, route, settings.parent / "ready.json")]
    assert not any("systemctl" in token or token == "killall" for command in all_commands for token in command)
    gemma, gateway = lab.commands(config, "translategemma", settings.parent / "ready.json")
    for key, value in (("--host", "127.0.0.1"), ("--threads", "4"), ("--threads-batch", "4"), ("--n-gpu-layers", "0"),
                       ("--ctx-size", "4096"), ("--batch-size", "128"), ("--parallel", "1")):
        assert gemma[gemma.index(key) + 1] == value
    assert "--jinja" in gemma and gateway[-1] == "18096"
    native = lab.commands(config, "madlad400_3b", settings.parent / "ready.json")[0]
    assert native[-2:] == ["--port", "18097"]


def test_spawn_private_logs_env_and_own_session(settings, lab, monkeypatch):
    config, plan = lab.load_config(settings)
    instance = lab.Lab(config, plan)
    instance.directory.mkdir()
    seen = {}
    process = SimpleNamespace(pid=1234, poll=lambda: None)
    def popen(args, **kwargs):
        seen.update(args=args, **kwargs)
        return process
    monkeypatch.setattr(lab.subprocess, "Popen", popen)
    assert instance.spawn(["private-lab-worker"], "route") is process
    assert seen["start_new_session"] is True and "shell" not in seen
    assert {seen["env"][key] for key in ("RAYON_NUM_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS")} == {"4"}
    assert seen["env"]["PYTHONPATH"].endswith("src")
    assert json.loads((instance.directory / "pids.local.json").read_text()) == [1234]
    instance.http.close()


def test_stop_uses_only_live_popen_group(lab, monkeypatch):
    killed = []
    monkeypatch.setattr(lab.os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    live = SimpleNamespace(pid=123, poll=lambda: None, wait=lambda timeout: 0)
    dead = SimpleNamespace(pid=456, poll=lambda: 0)
    lab.stop_owned(live)
    lab.stop_owned(dead)
    assert killed == [(123, lab.signal.SIGTERM)]


def test_loaded_capacity_needs_headroom_not_loading_threshold(settings, lab, monkeypatch):
    config, plan = lab.load_config(settings)
    instance = lab.Lab(config, plan)
    seen = []
    monkeypatch.setattr(instance, "status", lambda state, **fields: seen.append(fields))
    monkeypatch.setattr(instance, "sleeping", lambda _route: True)
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: "MemAvailable: 786432 kB\n" if path.as_posix() == "/proc/meminfo" else original(path, *args, **kwargs))
    instance.wait_resources("translategemma", loaded=True)
    assert seen[-1]["minimum_mb"] == 512
    interrupted = threading.Event()
    interrupted.set()
    with pytest.raises(lab.bench.AgentError, match="LAB_CAPACITY_INTERRUPTED"):
        instance.wait_resources("translategemma", loaded=True, interrupted=interrupted)
    instance.http.close()


def test_watchdog_foreign_wake_stops_only_owned_model(settings, lab, monkeypatch):
    config, plan = lab.load_config(settings)
    instance = lab.Lab(config, plan)
    owned, stopped, states = object(), [], []
    monkeypatch.setattr(instance, "sleeping", lambda _route: False)
    monkeypatch.setattr(instance, "status", lambda state, **_fields: states.append(state))
    monkeypatch.setattr(lab, "stop_owned", lambda process: stopped.append(process))
    interrupted = threading.Event()
    instance.watchdog(owned, SimpleNamespace(wait=lambda _seconds: False), interrupted)
    assert interrupted.is_set() and stopped == [owned] and states == ["capacity_interrupted"]
    instance.http.close()


@pytest.mark.parametrize("unknown", [False, True])
def test_prior_failure_or_unknown_stops_before_any_spawn(settings, lab, monkeypatch, unknown):
    config, plan = lab.load_config(settings)
    journal = lab.bench.Journal(config["output"], plan, resume=False)
    journal.append("task_started", task="hy_current:A00")
    if not unknown:
        journal.append("task_failed", task="hy_current:A00", uncertain=False, error="known")
    instance = lab.Lab(config, plan)
    monkeypatch.setattr(instance, "spawn", lambda *_args: pytest.fail("repeat"))
    with pytest.raises(lab.bench.AgentError, match="BENCHMARK_UNCERTAIN_TASK" if unknown else "LAB_PRIOR_TASK_FAILED"):
        instance.run()
    instance.http.close()


def test_stale_live_pid_blocks_without_adoption(settings, lab, monkeypatch):
    config, plan = lab.load_config(settings)
    instance = lab.Lab(config, plan)
    instance.directory.mkdir()
    (instance.directory / "pids.local.json").write_text("[123]")
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: True if path.as_posix() == "/proc/123" else exists(path))
    monkeypatch.setattr(lab, "stop_owned", lambda *_args: pytest.fail("adopted PID"))
    with pytest.raises(lab.bench.AgentError, match="LAB_OLD_PROCESS_PRESENT"):
        instance.run()
    instance.http.close()


@pytest.mark.parametrize("fail_first", [False, True, "known_model"])
def test_route_major_or_stop_at_first_failed_article(settings, lab, monkeypatch, fail_first):
    config, plan = lab.load_config(settings)
    instance = lab.Lab(config, plan)
    calls, capacity, owned, stopped = [], [], [], []
    monkeypatch.setattr(instance, "wait_resources", lambda route, **kwargs: capacity.append((route, kwargs.get("loaded", False))))
    monkeypatch.setattr(instance, "ready", lambda *_args: None)
    monkeypatch.setattr(instance, "watchdog", lambda *_args: None)
    def spawn(args, _name):
        process = SimpleNamespace(code=None, pid=10000 + len(owned))
        process.poll = lambda: process.code
        process.wait = lambda: 0
        if "--only-route" in args:
            route = args[args.index("--only-route") + 1]
            calls.append((route, args.copy()))
            journal = lab.bench.Journal(config["output"], plan, resume=config["output"].exists())
            article = next(item for item in plan["articles"] if route + ":" + item["id"] not in journal.tasks)
            task = route + ":" + article["id"]
            journal.append("task_started", task=task)
            failed = fail_first and (fail_first is True or len(calls) == 1)
            journal.append("task_failed" if failed else "task_completed", task=task, uncertain=False,
                           known_model_failure=failed and fail_first == "known_model")
            process.code = 0
        else:
            owned.append(process)
        instance.processes.append(process)
        return process
    def stop(process):
        if process.poll() is None:
            stopped.append(process)
            process.code = -15
    monkeypatch.setattr(instance, "spawn", spawn)
    monkeypatch.setattr(lab, "stop_owned", stop)
    if fail_first is True:
        with pytest.raises(lab.bench.AgentError, match="LAB_PRIOR_TASK_FAILED"):
            instance.run()
        assert [route for route, _ in calls] == ["hy_current"]
    else:
        instance.run()
        assert [route for route, _ in calls] == [route for route in lab.ORDER for _ in range(20)]
        assert len(owned) == 5
        assert json.loads((instance.directory / "status.local.json").read_text())["state"] == "completed"
    assert len(stopped) == len(owned) and {id(process) for process in stopped} == {id(process) for process in owned}
    assert all(args[args.index("--max-new-articles") + 1] == "1" for _, args in calls)
    assert "--resume" not in calls[0][1] and all("--resume" in args for _, args in calls[1:])
    assert len([item for item in capacity if not item[1]]) == (1 if fail_first is True else 4)
