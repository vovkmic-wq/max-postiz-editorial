"""Prepare one real LLM/Postiz draft for owner review; never approves or sends it."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from max_postiz_agent.adapters import extract_object
from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_cli import build
from max_postiz_agent.hub_models import BridgePost, Target


def main():
    root = Path(__file__).resolve().parents[1]
    record = root / "data/postiz-max-demo.json"
    core = build(root)
    try:
        with core.db.session() as s:
            if not all(t.paused for t in s.scalars(select(Target))):
                raise AgentError("PROBE_REQUIRES_ALL_TARGETS_PAUSED")
            target = s.get(Target, "max_test")
        if not target.options.get("postiz_integration_id"):
            raise AgentError("MAX_BRIDGE_NOT_CONNECTED")
        if record.exists():
            result = json.loads(record.read_text(encoding="utf-8"))
            core.postiz.get_post(result["postiz_post_id"])
        else:
            text, _ = core.editor.generate_text(
                'Верни только JSON с двумя строковыми полями title и body. Напиши короткий '
                'русский тестовый черновик для проверки связки редактора и календаря. '
                'Укажи, что это тестовый черновик, ожидающий проверки владельцем. '
                'Не утверждай, что он опубликован, не придумывай новости, цифры или факты. '
                'Текст до 400 символов. Не вызывай инструменты.')
            content = extract_object(text)
            if set(content) != {"title", "body"} or not all(isinstance(v, str) and v.strip() for v in content.values()):
                raise AgentError("PROBE_LLM_RESULT_INVALID")
            body = content["title"] + "\n\n" + content["body"]
            due = (datetime.now(UTC) + timedelta(days=1)).timestamp()
            remote = SimpleNamespace(platform="max-browser", destination=target.options["postiz_integration_id"], options={})
            post_id = core.postiz.create(remote, body, [], "draft", due)
            result = {"postiz_post_id": post_id, "body": body, "model": core.editor.model,
                      "state": "draft", "external_posts": 0, "owner_approved": False}
            record.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        core.bridge.sync(force=True)
        with core.db.session() as s:
            link = s.get(BridgePost, result["postiz_post_id"])
            if not link:
                raise AgentError("PROBE_BRIDGE_IMPORT_FAILED")
            result["variant_id"] = link.variant_id
        record.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"postiz_post_id": result["postiz_post_id"], "variant_id": result["variant_id"],
                          "model": result["model"], "state": "awaiting_owner_review", "external_posts": 0}))
    finally:
        core.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "code": exc.code if isinstance(exc, AgentError) else type(exc).__name__}))
        raise SystemExit(1) from None
