"""Loopback-адаптер лаборатории MADLAD: один фиксированный локальный worker."""

import argparse
import json
import os
import selectors
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class NativeTranslator:
    def __init__(self, args):
        if args.ready.exists():
            raise RuntimeError("NATIVE_READY_ALREADY_EXISTS")
        self.model = args.alias
        self.lock = threading.Lock()
        environment = {**os.environ, "RAYON_NUM_THREADS": "4", "OMP_NUM_THREADS": "4"}
        self.process = subprocess.Popen(
            [str(args.worker), "--model", str(args.model), "--tokenizer", str(args.tokenizer),
             "--config", str(args.config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            bufsize=1, env=environment,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        # Отдельный smoke не является одной из двадцати статей и не участвует
        # в сравнении качества. Он подтверждает загрузку до начала измерений.
        try:
            smoke = self.translate("The telescope observes stars.", max_new_tokens=128)
            if smoke["complete"] is not True or not smoke["text"].strip():
                raise RuntimeError("NATIVE_SMOKE_FAILED")
            args.ready.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(args.ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"smoke_only": True, "result": smoke}, stream, ensure_ascii=False)
        except Exception:
            self.close()
            raise

    def translate(self, text, max_new_tokens=1024):
        if self.process.poll() is not None:
            raise RuntimeError("NATIVE_WORKER_EXITED")
        started = time.monotonic()
        self.process.stdin.write(json.dumps({"text": text, "max_new_tokens": max_new_tokens}) + "\n")
        self.process.stdin.flush()
        if not self.selector.select(timeout=600):
            self.close()
            raise RuntimeError("NATIVE_WORKER_TIMEOUT")
        line = self.process.stdout.readline(1024 * 1024)
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise RuntimeError("NATIVE_RESPONSE_INVALID")
        return {"model": self.model, "text": value["text"], "complete": value.get("complete"),
                "metrics": {**{key: item for key, item in value.items() if key != "text"},
                            "known_model_failure": value.get("complete") is False and value.get("finish_reason") == "length",
                            "gateway_seconds": time.monotonic() - started}}

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class Handler(BaseHTTPRequestHandler):
    translator = None

    def log_message(self, *_args):
        pass  # Исходники и ответы сохраняет закрытый benchmark journal.

    def reply(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404,
                   {"status": "ok", "model": self.translator.model})

    def do_POST(self):
        if self.path != "/translate":
            return self.reply(404, {"error": "NOT_FOUND"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 24000:
                raise ValueError
            value = json.loads(self.rfile.read(length))
            if (not isinstance(value, dict) or set(value) != {"model", "text", "source", "target"}
                    or value["model"] != self.translator.model or value["source"] != "en"
                    or value["target"] != "ru" or not isinstance(value["text"], str)
                    or not 0 < len(value["text"].strip()) <= 1600):
                raise ValueError
        except (ValueError, TypeError):
            return self.reply(400, {"error": "INPUT_INVALID"})
        if not self.translator.lock.acquire(blocking=False):
            return self.reply(503, {"error": "BUSY"})
        try:
            self.reply(200, self.translator.translate(value["text"]))
        except Exception as exc:
            self.reply(502, {"error": type(exc).__name__})
        finally:
            self.translator.lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("worker", "model", "tokenizer", "config", "ready"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--alias", default="madlad400-3b-q4k")
    parser.add_argument("--port", type=int, default=18097)
    args = parser.parse_args()
    Handler.translator = NativeTranslator(args)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    finally:
        Handler.translator.close()


if __name__ == "__main__":
    main()
