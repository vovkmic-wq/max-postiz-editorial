"""Explicit prepare, inspected-hash approval, and read/status phases; never ticks workers."""
import argparse
import json
from pathlib import Path

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError, inside
from max_postiz_agent.hub_cli import build


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("article", help="Ignored project-local data/*.json with the real source text/date")
    publish = sub.add_parser("publish")
    publish.add_argument("record_id")
    publish.add_argument("--reviewed-hash", required=True)
    publish.add_argument("--due-at", type=float)
    status = sub.add_parser("status")
    status.add_argument("record_id", nargs="?")
    args = parser.parse_args()
    root, core = args.root.resolve(), None
    try:
        core = build(root)
        cycle = HourlyArticleCycle(core)
        if args.action == "prepare":
            path = inside(root, args.article)
            if not path.is_relative_to(root / "data") or path.suffix != ".json":
                raise AgentError("ARTICLE_PRIVATE_INPUT_REQUIRED")
            article = json.loads(path.read_text(encoding="utf-8-sig"))
            result = cycle.prepare(article)
        elif args.action == "publish":
            result = cycle.publish(args.record_id, args.reviewed_hash, args.due_at)
        else:
            result = cycle.status(args.record_id)
        print(json.dumps(result, ensure_ascii=False))
    except (AgentError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": exc.code if isinstance(exc, AgentError) else "ARTICLE_OPERATION_FAILED"}))
        raise SystemExit(1) from None
    finally:
        if core:
            core.close()


if __name__ == "__main__":
    main()
