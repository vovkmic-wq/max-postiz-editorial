"""Последовательный закрытый стенд: управляет только собственными процессами."""

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

import benchmark_articles as bench

PORTS = {"hy_current": 18094, "hy_glossary": 18095, "translategemma": 18096, "madlad400_3b": 18097}
ORDER = ("hy_current", "hy_glossary", "translategemma", "madlad400_3b")
PROPS = ("http://10.20.30.143:8086/props", "http://10.20.30.143:8089/props")


def load_config(path):
    """Команды собираются здесь; произвольных shell/argv/env в настройках нет."""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    bench.require(config.get("schema_version") == "translation-article-lab-v1", "LAB_CONFIG_INVALID")
    root = Path(config["root"]).resolve()
    for key in ("state_dir", "corpus", "routes", "output"):
        config[key] = bench.private_path(root, config[key])
    bench.require(Path(path).resolve().is_relative_to(root / "data"), "LAB_CONFIG_NOT_PRIVATE")
    config["root"] = root
    for key in ("python", "service", "native_gateway", "llama_server", "gemma_model", "gemma_template"):
        # venv/bin/python — symlink; resolve() теряет venv и его зависимости.
        config[key] = Path(config[key]).absolute() if key == "python" else Path(config[key]).resolve()
        bench.require(config[key].is_file(), "LAB_INPUT_MISSING")
    plan = bench.load_plan(root, config["corpus"], config["routes"])
    for route in ORDER:
        bench.require(plan["routes"][route]["base_url"] == f"http://127.0.0.1:{PORTS[route]}", "LAB_ENDPOINT_INVALID")
    for route in ORDER[:3]:
        item = bench.private_path(root, config["gateway_configs"][route])
        config["gateway_configs"][route] = item
        gateway = json.loads(item.read_text(encoding="utf-8"))
        alias = plan["routes"][route]["model"]
        backend = "http://10.20.30.143:8089" if route.startswith("hy_") else "http://127.0.0.1:18087"
        bench.require(gateway.get("gguf") == {alias: backend}, "LAB_BACKEND_INVALID")
        prompt = bench.private_path(root, gateway["prompt_path"])
        bench.require(bench.sha(prompt.read_bytes()) == plan["routes"][route]["profile"]["prompt_sha256"], "LAB_PROMPT_CHANGED")
        if route.startswith("hy_"):
            guard = gateway.get("memory_guard", {})
            bench.require(guard.get("model") == bench.HY_MODEL and guard.get("peer_props_url") == PROPS[0]
                          and guard.get("own_props_url") == PROPS[1], "LAB_MEMORY_GUARD_INVALID")
        else:
            bench.require("memory_guard" not in gateway, "LAB_MEMORY_GUARD_INVALID")
    bench.require(plan["routes"]["translategemma"]["model"] == "translategemma-12b-q4"
                  and plan["routes"]["madlad400_3b"]["model"] == "madlad400-3b-q4k", "LAB_ALIAS_INVALID")
    for key in ("worker", "model", "tokenizer", "config"):
        config["native"][key] = Path(config["native"][key]).resolve()
        bench.require(config["native"][key].is_file(), "LAB_INPUT_MISSING")
    resources = config["resources"]
    bench.require(set(resources["min_available_mb"]) == set(ORDER)
                  and all(type(value) is int and value >= 512 for value in resources["min_available_mb"].values())
                  and 1 <= resources["poll_seconds"] <= 30 and 1 <= resources["wait_timeout_seconds"] <= 86400, "LAB_RESOURCES_INVALID")
    return config, plan


def commands(config, route, ready):
    python, service = str(config["python"]), str(config["service"])
    gateway = [python, service, "--config", str(config["gateway_configs"].get(route, "")), "--port", str(PORTS[route])]
    if route == "madlad400_3b":
        return [[python, str(config["native_gateway"]), *[value for key, path in config["native"].items()
                 for value in ("--" + key, str(path))], "--ready", str(ready), "--port", "18097"]]
    if route == "translategemma":
        return [[str(config["llama_server"]), "--model", str(config["gemma_model"]), "--host", "127.0.0.1", "--port", "18087",
                 "--threads", "4", "--threads-batch", "4", "--n-gpu-layers", "0", "--ctx-size", "4096",
                 "--batch-size", "128", "--parallel", "1", "--jinja", "--chat-template-file", str(config["gemma_template"])], gateway]
    return [gateway]


def stop_owned(process):
    """Не принимаем PID из конфигурации/старого файла и не трогаем чужие службы."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # Процесс мог завершиться между poll и сигналом watchdog.
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


class Lab:
    def __init__(self, config, plan):
        self.config, self.plan, self.processes = config, plan, []
        self.directory = config["state_dir"]
        self.http = httpx.Client(timeout=3, trust_env=False, follow_redirects=False)
        self.environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                            "RAYON_NUM_THREADS": "4", "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"}

    def status(self, state, **fields):
        value = {"at": bench.stamp(), "state": state, "controller_pid": os.getpid(), **fields}
        raw = json.dumps(value, ensure_ascii=False) + "\n"
        with (self.directory / "events.local.jsonl").open("a", encoding="utf-8") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        (self.directory / "status.local.json").write_text(raw, encoding="utf-8")

    def spawn(self, args, name):
        log = (self.directory / (name + ".local.log")).open("ab")
        try:
            process = subprocess.Popen(args, env=self.environment, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        self.processes.append(process)
        (self.directory / "pids.local.json").write_text(json.dumps([p.pid for p in self.processes if p.poll() is None]), encoding="utf-8")
        return process

    def sleeping(self, route):
        urls = PROPS if route in {"translategemma", "madlad400_3b"} else PROPS[:1]
        try:
            return all(self.http.get(url).raise_for_status().json().get("is_sleeping") is True for url in urls)
        except (httpx.HTTPError, ValueError):
            return False

    def wait_resources(self, route, *, loaded=False, interrupted=None):
        started = time.monotonic()
        resources = self.config["resources"]
        minimum = 512 if loaded else resources["min_available_mb"][route]
        self.status("waiting_resources", route=route, minimum_mb=minimum, loaded=loaded)
        while time.monotonic() - started < resources["wait_timeout_seconds"]:
            bench.require(interrupted is None or not interrupted.is_set(), "LAB_CAPACITY_INTERRUPTED")
            memory = next(int(line.split()[1]) // 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
            if self.sleeping(route) and memory >= minimum:
                return
            time.sleep(resources["poll_seconds"])
        raise RuntimeError("LAB_RESOURCE_TIMEOUT")

    def ready(self, url, process):
        started = time.monotonic()
        while time.monotonic() - started < 600:
            bench.require(process.poll() is None, "LAB_PROCESS_EXITED")
            try:
                if self.http.get(url).status_code == 200:
                    self.status("ready", url=url, load_readiness_seconds=time.monotonic() - started)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("LAB_READINESS_TIMEOUT")

    def journal(self):
        if not self.config["output"].exists():
            return {}
        journal = bench.Journal(self.config["output"], self.plan, resume=True)
        bench.require(not any(event["kind"] == "task_failed" and
                      (event.get("known_model_failure") is not True or event.get("uncertain") is not False)
                      for event in journal.events), "LAB_PRIOR_TASK_FAILED")
        return journal.tasks

    def watchdog(self, model, finished, interrupted):
        while not finished.wait(1):
            if not self.sleeping("translategemma"):
                interrupted.set()
                self.status("capacity_interrupted", route="translategemma")
                stop_owned(model)
                return

    def run(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with bench.profile_lock(self.directory / "controller.lock"):
            old = self.directory / "pids.local.json"
            if old.exists():
                bench.require(not any(Path(f"/proc/{pid}").exists() for pid in json.loads(old.read_text())), "LAB_OLD_PROCESS_PRESENT")
            self.journal()
            self.status("started")
            try:
                for route in ORDER:
                    tasks = self.journal()
                    if all(route + ":" + article["id"] in tasks for article in self.plan["articles"]):
                        continue
                    self.wait_resources(route)
                    route_processes, finished, interrupted, watcher = [], threading.Event(), threading.Event(), None
                    try:
                        for index, args in enumerate(commands(self.config, route, self.directory / f"native-ready-{time.time_ns()}.local.json")):
                            process = self.spawn(args, route + "-" + str(index))
                            route_processes.append(process)
                            if route == "translategemma" and index == 0:
                                watcher = threading.Thread(target=self.watchdog, args=(process, finished, interrupted), daemon=True)
                                watcher.start()
                            endpoint = "http://127.0.0.1:18087" if route == "translategemma" and index == 0 else f"http://127.0.0.1:{PORTS[route]}"
                            self.ready(endpoint + "/health", process)
                        for _ in range(20):
                            tasks = self.journal()
                            if all(route + ":" + article["id"] in tasks for article in self.plan["articles"]):
                                break
                            self.wait_resources(route, loaded=True, interrupted=interrupted)
                            bench.require(not interrupted.is_set(), "LAB_CAPACITY_INTERRUPTED")
                            args = [str(self.config["python"]), str(Path(bench.__file__).resolve()), "--root", str(self.config["root"]),
                                    "--corpus", str(self.config["corpus"]), "--routes", str(self.config["routes"]), "--output", str(self.config["output"]),
                                    "--only-route", route, "--max-new-articles", "1", "--execute"]
                            if self.config["output"].exists():
                                args.append("--resume")
                            self.status("article_running", route=route)
                            process = self.spawn(args, route + "-articles")
                            bench.require(process.wait() == 0 and not interrupted.is_set(), "LAB_ARTICLE_FAILED")
                            self.journal()
                    finally:
                        finished.set()
                        if watcher:
                            watcher.join(timeout=10)
                        for process in reversed(route_processes):
                            stop_owned(process)
                self.status("completed")
            except BaseException as exc:
                self.status("failed", error=type(exc).__name__, detail=str(exc))
                raise
            finally:
                for process in reversed(self.processes):
                    stop_owned(process)
                old.write_text("[]", encoding="utf-8")
                self.http.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    config, plan = load_config(args.config)
    if args.execute:
        def terminate(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, terminate)
        Lab(config, plan).run()
    else:
        print(json.dumps({"dry_run": True, "order": ORDER, "articles_per_route": 20, "contract": plan["contract"]}))


if __name__ == "__main__":
    main()
