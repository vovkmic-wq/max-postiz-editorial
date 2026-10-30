"""Закрытый MADLAD EN→RU: исправленная сборка, одна попытка и освобождение RAM."""

import argparse
import base64
import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

MODEL = "madlad400-3b-q4k-causal-v2"
CONTRACT = "madlad400-candle-causal-v2"
ARTIFACT_SHA256 = {
    "worker": "7b3fd9c77e130992a9b233c30b26a0245b9bb4386a5ecc7250f244d90d4e97cd",
    "model": "ea6e5531a3e95213c7f0635988d119e078a655c09306e47851e15d4c0c3f9c37",
    "tokenizer": "a2799ccc696b752ba00c34f58726bfe253a04921ceb6cfc620400f560474790b",
    "config": "cad399cab799b99409a6ec2d90d72552257c2bb752861261d2016691e0643e7c",
}
PEER_PROPS = ("http://10.20.30.143:8086/props", "http://10.20.30.143:8089/props")
MAX_NEW_TOKENS = 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class ServiceError(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def require(condition, code="MADLAD_CONFIG_INVALID", status=503):
    if not condition:
        raise ServiceError(code, status)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def signature(path):
    item = path.stat()
    require(stat.S_ISREG(item.st_mode), "MADLAD_ARTIFACT_INVALID")
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)


def write_private(path, value):
    """События не перезаписываются; raw и claims остаются после ошибки процесса."""
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def sync_directory(path):
    # Linux fsync каталога сохраняет создание claim при аварийном отключении.
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        require(isinstance(config, dict) and set(config) == {
            "schema_version", "model", "artifacts", "state_dir", "resources"})
        require(config["schema_version"] == "madlad-service-v1" and config["model"] == MODEL)
        artifacts = config["artifacts"]
        require(isinstance(artifacts, dict) and set(artifacts) == set(ARTIFACT_SHA256))
        paths, signatures = {}, {}
        for name, digest in ARTIFACT_SHA256.items():
            artifact = artifacts[name]
            require(isinstance(artifact, dict) and set(artifact) == {"path", "sha256"}
                    and artifact["sha256"] == digest and isinstance(artifact["path"], str)
                    and Path(artifact["path"]).is_absolute())
            target = Path(artifact["path"]).resolve(strict=True)
            before = signature(target)
            hashed = hashlib.sha256()
            with target.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    hashed.update(block)
            require(hashed.hexdigest() == digest, "MADLAD_ARTIFACT_HASH_MISMATCH")
            require(signature(target) == before, "MADLAD_ARTIFACT_CHANGED")
            paths[name], signatures[name] = target, before
        resources = config["resources"]
        require(isinstance(resources, dict) and set(resources) == {
            "min_available_mb", "peer_wait_seconds", "request_timeout_seconds", "poll_seconds"})
        require(type(resources["min_available_mb"]) is int and resources["min_available_mb"] >= 6144)
        for name, maximum in (("peer_wait_seconds", 180), ("request_timeout_seconds", 570),
                              ("poll_seconds", 2)):
            require(type(resources[name]) in {int, float} and math.isfinite(resources[name])
                    and 0 < resources[name] <= maximum)
        require(resources["poll_seconds"] <= resources["peer_wait_seconds"]
                < resources["request_timeout_seconds"])
        state = Path(config["state_dir"])
        # Только закрытый data; исходники и системные журналы не получают статьи.
        # Внешний data в staging не должен скрывать выход через data/../outside.
        require(state.is_absolute() and ".." not in state.parts
                and "data" in state.parts and len(state.parts) >= 3)
        state = state.resolve()
        require("data" in state.parts and len(state.parts) >= 3)
        return {"paths": paths, "signatures": signatures, "resources": resources, "state": state}
    except ServiceError:
        raise
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise ServiceError("MADLAD_CONFIG_INVALID") from exc


def valid_input(value):
    return (isinstance(value, dict) and set(value) == {"model", "text", "source", "target"}
            and value["model"] == MODEL and value["source"] == "en" and value["target"] == "ru"
            and isinstance(value["text"], str) and 0 < len(value["text"].strip())
            and len(value["text"]) <= 1600)


class Translator:
    def __init__(self, config, *, client=None, clock=time.monotonic, sleep=time.sleep,
                 process_factory=subprocess.Popen, memory_path=Path("/proc/meminfo")):
        self.config, self.clock, self.sleep = config, clock, sleep
        self.process_factory, self.memory_path = process_factory, memory_path
        self.lock, self.stopping = threading.Lock(), threading.Event()
        self.process = None
        self.http = client or httpx.Client(timeout=3, trust_env=False, follow_redirects=False)
        self.state = config["state"]
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.requests = self.state / "requests"
        self.requests.mkdir(exist_ok=True, mode=0o700)
        self.instance_lock = None
        if os.name == "posix":
            import fcntl
            self.instance_lock = (self.state / "service.lock").open("ab")
            try:
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self.instance_lock.close()
                self.http.close()
                raise ServiceError("MADLAD_SERVICE_ALREADY_RUNNING") from exc
        self.quarantine_path = self.state / "quarantine.local.json"
        self.quarantined = self.quarantine_path.exists()
        # Потерянный ответ после рестарта остаётся неизвестным, а не новой попыткой.
        unresolved = [item.name for item in self.requests.iterdir() if item.is_dir()
                      and not self._resolved(item)]
        if unresolved and not self.quarantine_path.exists():
            write_private(self.quarantine_path, {"code": "MADLAD_PRIOR_REQUEST_UNRESOLVED",
                                                "requests": unresolved, "at": time.time()})
            self.quarantined = True

    @classmethod
    def _resolved(cls, directory):
        # Само наличие файла после power loss не доказывает завершённую запись.
        try:
            terminal = json.loads((directory / "terminal.local.json").read_text(encoding="utf-8"))
            claim = json.loads((directory / "claim.local.json").read_text(encoding="utf-8"))
            result = terminal["result"]
            metrics = result["metrics"]
            raw = base64.b64decode(metrics["raw_worker_base64"], validate=True)
            worker = cls._validate(raw)
            return (terminal["outcome"] == "returned" and result["model"] == MODEL
                    and type(result["complete"]) is bool and valid_input(claim["request"])
                    and result["text"] == worker["text"]
                    and result["complete"] == (worker["complete"] and bool(worker["text"].strip()))
                    and metrics["request_id"] == claim["request_id"] == directory.name
                    and metrics["backend_contract"] == claim["backend_contract"] == CONTRACT
                    and metrics["artifact_sha256"] == claim["artifact_sha256"] == ARTIFACT_SHA256
                    and metrics["raw_worker_sha256"] == sha(raw))
        except (OSError, ValueError, KeyError, TypeError, ServiceError):
            return False

    def _unchanged(self):
        require(all(signature(path) == self.config["signatures"][name]
                    for name, path in self.config["paths"].items()), "MADLAD_ARTIFACT_CHANGED")

    def _available_mb(self):
        try:
            return next(int(line.split()[1]) // 1024 for line in
                        self.memory_path.read_text().splitlines() if line.startswith("MemAvailable:"))
        except (OSError, ValueError, StopIteration) as exc:
            raise ServiceError("MADLAD_MEMORY_STATE_UNAVAILABLE") from exc

    def _peers_sleeping(self):
        try:
            for url in PEER_PROPS:
                response = self.http.get(url)
                if response.status_code == 503:
                    return False  # Чужая модель загружается; мы только ждём.
                response.raise_for_status()
                require(len(response.content) <= 65536, "MADLAD_PEER_STATE_INVALID")
                value = response.json()
                require(isinstance(value, dict) and type(value.get("is_sleeping")) is bool,
                        "MADLAD_PEER_STATE_INVALID")
                if value["is_sleeping"] is False:
                    return False
            return True
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceError("MADLAD_PEER_STATE_UNAVAILABLE") from exc

    def _wait_idle(self, deadline):
        started = self.clock()
        limit = min(deadline, started + self.config["resources"]["peer_wait_seconds"])
        while self.clock() < limit and not self.stopping.is_set():
            if self._peers_sleeping() and self._available_mb() >= self.config["resources"]["min_available_mb"]:
                return self.clock() - started
            self.sleep(min(self.config["resources"]["poll_seconds"], max(0, limit - self.clock())))
        raise ServiceError("MADLAD_RESOURCE_WAIT_TIMEOUT")

    def _capacity(self):
        require(not self.stopping.is_set(), "MADLAD_STOPPED")
        require(self._peers_sleeping() and self._available_mb() >= 256,
                "MADLAD_CAPACITY_INTERRUPTED")

    @staticmethod
    def _stop(process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()

    def _read_response(self, process, deadline, directory):
        """Читаем байты с общим deadline; частичная строка не продлевает ожидание."""
        output = bytearray()
        selector = selectors.DefaultSelector()
        # В том числе частичный ответ остаётся на диске при timeout/crash.
        descriptor = os.open(directory / "worker-stream.local.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while self.clock() < deadline:
                self._capacity()
                require(process.poll() is None, "MADLAD_WORKER_EXITED")
                if not selector.select(timeout=min(1, max(0, deadline - self.clock()))):
                    continue
                block = os.read(process.stdout.fileno(), 65536)
                require(bool(block), "MADLAD_WORKER_EXITED")
                require(os.write(descriptor, block) == len(block), "MADLAD_RAW_WRITE_FAILED")
                os.fsync(descriptor)
                output.extend(block)
                require(len(output) <= MAX_RESPONSE_BYTES, "MADLAD_RESPONSE_TOO_LARGE")
                if b"\n" in output:
                    require(output.count(b"\n") == 1 and output.endswith(b"\n"),
                            "MADLAD_RESPONSE_INVALID")
                    return bytes(output)
            raise ServiceError("MADLAD_WORKER_TIMEOUT")
        finally:
            os.close(descriptor)
            sync_directory(directory)
            selector.close()

    @staticmethod
    def _validate(raw):
        try:
            value = json.loads(raw)
            expected = {"text", "complete", "finish_reason", "source_tokens", "output_tokens",
                        "token_ids", "load_seconds", "inference_seconds", "cold_start"}
            require(isinstance(value, dict) and set(value) in (expected, expected | {"error"}),
                    "MADLAD_RESPONSE_INVALID")
            require(isinstance(value["text"], str) and type(value["complete"]) is bool
                    and type(value["cold_start"]) is bool
                    and type(value["source_tokens"]) is int and value["source_tokens"] > 0
                    and type(value["output_tokens"]) is int
                    and isinstance(value["token_ids"], list)
                    and value["output_tokens"] == len(value["token_ids"]) <= MAX_NEW_TOKENS
                    and all(type(token) is int and 0 <= token < 2 ** 32 for token in value["token_ids"]),
                    "MADLAD_RESPONSE_INVALID")
            require(all(type(value[key]) in (int, float) and math.isfinite(value[key]) and value[key] >= 0
                        for key in ("load_seconds", "inference_seconds")), "MADLAD_RESPONSE_INVALID")
            reason = value["finish_reason"]
            if value["complete"]:
                require(reason == "EOS" and value["token_ids"] and value["token_ids"][-1] == 2
                        and 2 not in value["token_ids"][:-1]
                        and "error" not in value, "MADLAD_RESPONSE_INVALID")
            elif reason == "length":
                require(len(value["token_ids"]) == MAX_NEW_TOKENS and 2 not in value["token_ids"]
                        and "error" not in value, "MADLAD_RESPONSE_INVALID")
            else:
                require(reason == "error" and value.get("error") in
                        {"INVALID_REQUEST", "INFERENCE_FAILED", "DECODE_FAILED"}, "MADLAD_RESPONSE_INVALID")
            return value
        except (TypeError, ValueError, KeyError) as exc:
            raise ServiceError("MADLAD_RESPONSE_INVALID") from exc

    def health(self):
        try:
            self._unchanged()
            valid = True
        except (ServiceError, OSError):
            valid = False
        blocked = self.quarantined or self.quarantine_path.exists() or not valid
        return (503 if blocked else 200), {
            "status": "blocked" if blocked else "configured", "model": MODEL,
            "backend_contract": CONTRACT, "artifacts_verified": valid,
            "inference_verified": False, "model_loaded": self.process is not None,
            "busy": self.lock.locked(), "quarantined": self.quarantined or self.quarantine_path.exists()}

    def translate(self, request):
        require(valid_input(request), "INPUT_INVALID", 400)
        require(self.lock.acquire(blocking=False), "BUSY")
        process, directory = None, None
        try:
            require(not self.quarantined and not self.quarantine_path.exists(), "MADLAD_RECONCILIATION_REQUIRED")
            self._unchanged()
            started = self.clock()
            deadline = started + self.config["resources"]["request_timeout_seconds"]
            waited = self._wait_idle(deadline)
            self._unchanged()
            ident = str(uuid.uuid4())
            directory = self.requests / ident
            directory.mkdir(mode=0o700)
            sync_directory(self.requests)
            write_private(directory / "claim.local.json", {
                "request_id": ident, "at": time.time(), "request": request,
                "request_sha256": sha(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()),
                "artifact_sha256": ARTIFACT_SHA256, "backend_contract": CONTRACT})
            paths = self.config["paths"]
            environment = {**os.environ, "RAYON_NUM_THREADS": "4", "OMP_NUM_THREADS": "4",
                           "OPENBLAS_NUM_THREADS": "4"}
            process = self.process_factory(
                [str(paths["worker"]), "--model", str(paths["model"]), "--tokenizer", str(paths["tokenizer"]),
                 "--config", str(paths["config"])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=environment)
            self.process = process
            write_private(directory / "started.local.json", {"at": time.time(), "owned_pid": process.pid})
            process.stdin.write(json.dumps({"text": request["text"], "max_new_tokens": MAX_NEW_TOKENS},
                                           ensure_ascii=False).encode("utf-8") + b"\n")
            process.stdin.flush()
            raw = self._read_response(process, deadline, directory)
            # Raw записан до проверки протокола и не теряется при отрицательном исходе.
            write_private(directory / "raw.local.json", {"sha256": sha(raw),
                                                          "base64": base64.b64encode(raw).decode("ascii")})
            value = self._validate(raw)
            self._stop(process)
            self.process = process = None
            complete = value["complete"] is True and bool(value["text"].strip())
            reason = "empty" if value["complete"] and not complete else value["finish_reason"]
            result = {"model": MODEL, "text": value["text"], "complete": complete,
                      "metrics": {**{key: item for key, item in value.items() if key not in {"text", "complete"}},
                          "finish_reason": reason, "known_model_failure": reason in {"length", "empty"},
                          "request_id": ident, "backend_contract": CONTRACT, "artifact_sha256": ARTIFACT_SHA256,
                          "raw_worker_sha256": sha(raw), "raw_worker_base64": base64.b64encode(raw).decode("ascii"),
                          "memory_guard": {"peer_wait_seconds": waited, "released_before_response": True},
                          "gateway_seconds": self.clock() - started}}
            write_private(directory / "terminal.local.json", {"at": time.time(), "outcome": "returned",
                                                               "result": result})
            return result
        except BaseException as exc:
            # Известные length/empty уже вернулись как complete=false. Здесь лишь
            # неизвестный/повреждённый исход после claim: повтор не разрешается.
            if directory is not None:
                self.quarantined = True
                code = exc.code if isinstance(exc, ServiceError) else "MADLAD_NATIVE_UNKNOWN"
                if not self.quarantine_path.exists():
                    try:
                        write_private(self.quarantine_path, {"request_id": directory.name,
                                                            "code": code, "at": time.time()})
                    except OSError:
                        # Даже при полном диске текущий процесс блокирует новые
                        # запросы; после рестарта остаётся незавершённый каталог.
                        raise ServiceError("MADLAD_EVIDENCE_WRITE_FAILED") from exc
            if isinstance(exc, (ServiceError, KeyboardInterrupt, SystemExit)):
                raise
            raise ServiceError("MADLAD_NATIVE_UNKNOWN") from exc
        finally:
            try:
                self._stop(process)
            finally:
                self.process = None
                self.lock.release()

    def close(self):
        self.stopping.set()
        self._stop(self.process)
        self.http.close()
        if self.instance_lock is not None:
            self.instance_lock.close()


class Handler(BaseHTTPRequestHandler):
    translator = None

    def log_message(self, *_args):
        pass  # Никаких исходников, переводов и cookies в системном журнале.

    def reply(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/health":
            return self.reply(404, {"error": "NOT_FOUND"})
        return self.reply(*self.translator.health())

    def do_POST(self):
        if self.path != "/translate":
            return self.reply(404, {"error": "NOT_FOUND"})
        try:
            self.connection.settimeout(10)
            require("Transfer-Encoding" not in self.headers, "INPUT_INVALID", 400)
            require(len(self.headers.get_all("Content-Length", [])) == 1, "INPUT_INVALID", 400)
            length = int(self.headers.get("Content-Length", "0"))
            require(0 < length <= 24000, "INPUT_INVALID", 400)
            value = json.loads(self.rfile.read(length))
            require(valid_input(value), "INPUT_INVALID", 400)
        except (ServiceError, ValueError, OSError):
            return self.reply(400, {"error": "INPUT_INVALID", "retryable": False})
        try:
            self.reply(200, self.translator.translate(value))
        except ServiceError as exc:
            self.reply(exc.status, {"error": exc.code, "retryable": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    translator = Translator(load_config(args.config))
    Handler.translator = translator
    server = ThreadingHTTPServer(("127.0.0.1", 18099), Handler)
    try:
        def terminate(*_args):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, terminate)
        server.serve_forever()
    finally:
        translator.close()
        server.server_close()


if __name__ == "__main__":
    main()
