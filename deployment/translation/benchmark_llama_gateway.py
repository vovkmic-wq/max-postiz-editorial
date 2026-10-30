"""Прежний переводчик с полной диагностикой ответов только для лаборатории."""

import argparse
import base64
import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from service import Handler, Translator


class AuditedTranslator(Translator):
    """Не меняет prompt, sampling, memory guards или завершённый перевод."""

    def __init__(self, config):
        super().__init__(config)
        self.backend = None
        self.http.event_hooks["response"].append(self.capture)

    def capture(self, response):
        if response.request.url.path != "/v1/chat/completions":
            return
        raw = response.read()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("BENCHMARK_BACKEND_RESPONSE_TOO_LARGE")
        self.backend = {"status": response.status_code,
                        "request_sha256": hashlib.sha256(response.request.content).hexdigest(),
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "raw_base64": base64.b64encode(raw).decode("ascii")}

    def translate(self, model, text):
        self.backend = None
        try:
            result = super().translate(model, text)
        except ValueError as exc:
            # Известный незавершённый ответ — измеряемый FAIL, не одобрение
            # и не повод выбирать лучший повтор. Остальные ошибки остаются 502.
            if (str(exc) not in {"TRANSLATION_TRUNCATED", "TRANSLATION_EMPTY"}
                    or self.backend is None or self.backend["status"] != 200):
                raise
            data = json.loads(base64.b64decode(self.backend["raw_base64"]))
            choice = data["choices"][0]
            output = choice["message"]["content"]
            finish = choice["finish_reason"]
            if not isinstance(output, str):
                raise ValueError("BENCHMARK_BACKEND_RESPONSE_INVALID") from exc
            known = (finish == "length" or finish == "stop" and not output.strip())
            if not known:
                raise
            result = {"model": model, "text": output, "complete": False,
                      "metrics": {"known_model_failure": True,
                                  "finish_reason": "length" if finish == "length" else "empty",
                                  "timings": data.get("timings"), "usage": data.get("usage")}}
        result["metrics"]["backend_response"] = self.backend
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    Handler.translator = AuditedTranslator(json.loads(args.config.read_text(encoding="utf-8")))
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    finally:
        Handler.translator.http.close()


if __name__ == "__main__":
    main()
