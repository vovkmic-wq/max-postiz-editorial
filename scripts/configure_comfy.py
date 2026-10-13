"""Register an operator-provided ComfyUI workflow without contacting the generator."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from max_postiz_agent.config import AgentError, fingerprint, inside, parse_json


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--positive-node", required=True)
    parser.add_argument("--positive-input", default="text")
    parser.add_argument("--negative-node")
    parser.add_argument("--seed-node")
    parser.add_argument("--seed-input", default="seed")
    parser.add_argument("--frames-node")
    parser.add_argument("--frames-input", default="length")
    parser.add_argument("--fps", type=int, default=16)
    parser.add_argument(
        "--local-zero-cost",
        action="store_true",
        help="Explicitly assert that the approved workflow has no paid API calls",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    parsed = urlsplit(args.url)
    # Обрабатываем ветвь при условии: parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or pars….
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AgentError("GENERATOR_URL_INVALID")
    workflow = parse_json(inside(root, args.workflow).read_text(encoding="utf-8-sig"))
    # Обрабатываем ветвь при условии: not isinstance(workflow, dict) or not workflow or any((not isinstance(n, dict) or 'class_type' not in n or ….
    if (
        not isinstance(workflow, dict)
        or not workflow
        or any(
            not isinstance(n, dict)
            or "class_type" not in n
            or not isinstance(n.get("inputs"), dict)
            for n in workflow.values()
        )
    ):
        raise AgentError("GENERATOR_WORKFLOW_INVALID")
    bindings = [{"node": args.positive_node, "input": args.positive_input, "value": "positive"}]
    # Последовательно обрабатываем элементы: [(args.negative_node, 'text', 'negative'), (args.seed_node, args.seed_input, 'seed'), (args.frames_node, ar….
    for node, field, value in [
        (args.negative_node, "text", "negative"),
        (args.seed_node, args.seed_input, "seed"),
        (args.frames_node, args.frames_input, "frames"),
    ]:
        # Обрабатываем ветвь при условии: node.
        if node:
            bindings.append({"node": node, "input": field, "value": value})
    # Последовательно обрабатываем элементы: bindings.
    for binding in bindings:
        # Обрабатываем ветвь при условии: binding['node'] not in workflow or binding['input'] not in workflow[binding['node']]['inputs'].
        if (
            binding["node"] not in workflow
            or binding["input"] not in workflow[binding["node"]]["inputs"]
        ):
            raise AgentError("WORKFLOW_BINDING_INVALID")
    path = inside(root, "config/comfy/workflow.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
    app_path = root / "config/app.json"
    app = parse_json(app_path.read_text(encoding="utf-8-sig"))
    app["video"].update(
        backend="comfyui",
        workflow_path="config/comfy/workflow.json",
        workflow_sha256=fingerprint(workflow),
        allowed_node_classes=sorted({n["class_type"] for n in workflow.values()}),
        bindings=bindings,
        generation_fps=args.fps,
        local_zero_cost=args.local_zero_cost,
    )
    app_path.write_text(json.dumps(app, ensure_ascii=False, indent=2), encoding="utf-8")
    env = root / ".env.local"
    lines = env.read_text(encoding="utf-8-sig").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith("COMFYUI_BASE_URL=")]
    env.write_text(
        "\n".join(lines) + "\nCOMFYUI_BASE_URL=" + args.url.rstrip("/") + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"configured": True, "workflow_sha256": fingerprint(workflow), "restart_required": True}
        )
    )


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        main()
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        print(json.dumps({"code": exc.code}))
        raise SystemExit(1) from None
