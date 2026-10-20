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
    prepare.add_argument("--retry-record", help="Explicit operator retry of this same failed pre-draft record")
    prepare.add_argument("--candidate", help="Ignored Russian title/body JSON for an existing failed pre-draft retry")
    rejected = sub.add_parser("stage-rejected")
    rejected.add_argument("record_id")
    rejected.add_argument("article", help="The unchanged ignored article input for the completed rejected trace")
    rejected.add_argument("--restore-cancelled", action="store_true")
    rejected.add_argument("--reason", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("record_id")
    publish.add_argument("--reviewed-hash", required=True)
    publish.add_argument("--due-at", type=float)
    migrate = sub.add_parser("migrate-automatic")
    migrate.add_argument("record_id")
    migrate.add_argument("--reason", required=True)
    correct = sub.add_parser("correct-staged")
    correct.add_argument("record_id")
    correct.add_argument("candidate", help="Changed ignored Russian title/body JSON for the same exact DRAFT")
    correct.add_argument("--reviewed-hash", required=True)
    correct.add_argument("--reason", required=True)
    reconcile = sub.add_parser("reconcile-approval")
    reconcile.add_argument("record_id")
    reconcile.add_argument("--reviewed-hash", required=True)
    reconcile.add_argument("--due-at", type=float, required=True)
    reconcile.add_argument("--reason", required=True)
    protocol = sub.add_parser("recover-review-protocol")
    protocol.add_argument("record_id")
    protocol.add_argument("article", help="The unchanged ignored source article JSON")
    protocol.add_argument("candidate", help="The unchanged ignored Russian candidate JSON")
    protocol.add_argument("--trace-sha256", required=True)
    protocol.add_argument("--reason", required=True)
    status = sub.add_parser("status")
    status.add_argument("record_id", nargs="?")
    cancel = sub.add_parser("cancel-preparation")
    cancel.add_argument("record_id")
    cancel.add_argument("--reason", default="Operator reconciled and abandoned pre-draft preparation")
    abandon = sub.add_parser("abandon-prepared")
    abandon.add_argument("record_id")
    abandon.add_argument("--reason", default="Operator rejected an unpublished local article draft")
    revise = sub.add_parser("revise-prepared")
    revise.add_argument("record_id")
    revise.add_argument("correction", help="Ignored project-local JSON with Russian title/body and no footer")
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
            candidate = None
            if args.candidate:
                candidate_path = inside(root, args.candidate)
                if not candidate_path.is_relative_to(root / "data") or candidate_path.suffix != ".json":
                    raise AgentError("ARTICLE_PRIVATE_INPUT_REQUIRED")
                candidate = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
            result = cycle.prepare(article, retry_record=args.retry_record, candidate=candidate)
        elif args.action == "stage-rejected":
            path = inside(root, args.article)
            if not path.is_relative_to(root / "data") or path.suffix != ".json":
                raise AgentError("ARTICLE_PRIVATE_INPUT_REQUIRED")
            result = cycle.stage_rejected(args.record_id, json.loads(path.read_text(encoding="utf-8-sig")),
                restore_cancelled=args.restore_cancelled, reason=args.reason)
        elif args.action == "publish":
            result = cycle.publish(args.record_id, args.reviewed_hash, args.due_at)
        elif args.action == "migrate-automatic":
            result = cycle.migrate_automatic(args.record_id, args.reason)
        elif args.action == "correct-staged":
            path = inside(root, args.candidate)
            if not path.is_relative_to(root / "data") or path.suffix != ".json":
                raise AgentError("ARTICLE_PRIVATE_INPUT_REQUIRED")
            result = cycle.correct_staged(args.record_id, json.loads(path.read_text(encoding="utf-8-sig")),
                                          args.reviewed_hash, args.reason)
        elif args.action == "reconcile-approval":
            result = cycle.reconcile_approval(args.record_id, args.reviewed_hash, args.due_at, args.reason)
        elif args.action == "recover-review-protocol":
            paths = [inside(root, value) for value in (args.article, args.candidate)]
            if any(not path.is_relative_to(root / "data") or path.suffix != ".json" for path in paths):
                raise AgentError("ARTICLE_PRIVATE_INPUT_REQUIRED")
            article, candidate = [json.loads(path.read_text(encoding="utf-8-sig")) for path in paths]
            result = cycle.recover_review_protocol(args.record_id, article, candidate, args.trace_sha256, args.reason)
        elif args.action == "cancel-preparation":
            result = cycle.cancel_preparation(args.record_id, args.reason)
        elif args.action == "abandon-prepared":
            result = cycle.abandon_prepared(args.record_id, args.reason)
        elif args.action == "revise-prepared":
            path = inside(root, args.correction)
            if not path.is_relative_to(root / "data") or path.suffix != ".json":
                raise AgentError("ARTICLE_PRIVATE_INPUT_REQUIRED")
            result = cycle.revise_prepared(args.record_id, json.loads(path.read_text(encoding="utf-8-sig")))
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
