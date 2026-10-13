"""Probe a real ComfyUI server; optional stock graph does not generate AI content."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"url": args.url, "ai_generation_verified": False}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with httpx.Client(base_url=args.url.rstrip("/"), trust_env=False, timeout=30) as client:
        stats = client.get("/system_stats")
        stats.raise_for_status()
        result["system"] = stats.json()
        queue = client.get("/queue")
        queue.raise_for_status()
        result["queue_api"] = True
        info = client.get("/object_info")
        info.raise_for_status()
        nodes = info.json()
        result["node_count"] = len(nodes)
        result["stock_video_nodes"] = [n for n in ("CreateVideo", "SaveVideo") if n in nodes]
        # Обрабатываем ветвь при условии: args.exercise.
        if args.exercise:
            # Deliberately no checkpoint, paid provider or generative model.
            graph = {
                "1": {"class_type": "EmptyImage", "inputs": {
                    "width": 64, "height": 64, "batch_size": 1, "color": 3368652}},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": ["1", 0], "filename_prefix": "MAX_server_technical_probe"}},
            }
            submitted = client.post("/prompt", json={"prompt": graph, "client_id": str(uuid.uuid4())})
            submitted.raise_for_status()
            prompt_id = submitted.json()["prompt_id"]
            result["technical_prompt_id"] = prompt_id
            # Последовательно обрабатываем элементы: range(60).
            for _ in range(60):
                history = client.get("/history/" + prompt_id)
                history.raise_for_status()
                record = history.json().get(prompt_id)
                # Обрабатываем ветвь при условии: record.
                if record:
                    # Обрабатываем ветвь при условии: record.get('status', {}).get('status_str') == 'error'.
                    if record.get("status", {}).get("status_str") == "error":
                        raise RuntimeError("Technical graph failed")
                    files = record.get("outputs", {}).get("2", {}).get("images", [])
                    # Обрабатываем ветвь при условии: files.
                    if files:
                        artifact = client.get("/view", params=files[0])
                        artifact.raise_for_status()
                        # Обрабатываем ветвь при условии: not artifact.content.startswith(b'\x89PNG\r\n\x1a\n').
                        if not artifact.content.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise RuntimeError("Unexpected artifact format")
                        result["technical_graph_verified"] = True
                        result["artifact_bytes"] = len(artifact.content)
                        break
                time.sleep(1)
            else:
                # Альтернативная ветвь после проверки основного условия.
                raise RuntimeError("Technical graph timed out; do not automatically resubmit")
    # Обрабатываем ветвь при условии: args.output.
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    main()
