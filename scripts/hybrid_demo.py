"""Two technical FFmpeg scenes -> real montage -> review, with no AI or external posting."""

import json
import shutil
from pathlib import Path

from max_postiz_agent.cli import build_service, initialize
from max_postiz_agent.config import read_topic
from max_postiz_agent.video_tools import ffmpeg

root = Path(__file__).resolve().parents[1]
demo = root / "data/hybrid-demo"
demo.mkdir(parents=True, exist_ok=True)
# Последовательно обрабатываем элементы: ('config', 'schemas', 'prompts').
for folder in ("config", "schemas", "prompts"):
    shutil.copytree(
        root / folder,
        demo / folder,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("app.json", "sources.json", "secrets.env", "workflow.json"),
    )
shutil.copyfile(root / "TOPIC.md", demo / "TOPIC.md")
initialize(demo)
app = json.loads((demo / "config/app.json").read_text(encoding="utf-8-sig"))
app["opencode"]["adapter_mode"] = "fake"
app["max"]["adapter_mode"] = "fake"
app["video"].update(width=160, height=288)
(demo / "config/app.json").write_text(json.dumps(app), encoding="utf-8")
service = build_service(demo)
# Выполняем операцию с отдельной обработкой возможных ошибок.
try:
    # Последовательно обрабатываем элементы: enumerate(('blue', 'red')).
    for index, colour in enumerate(("blue", "red")):
        ffmpeg(
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s=160x288:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(demo / f"scene{index}.mp4"),
            ]
        )
    plan = {
        "title": "Техническая демонстрация монтажа",
        "caption": "Демонстрация обработки двух технических сцен и подготовки ролика к проверке.",
        "rights_basis": "Собственные технические тестовые материалы",
        "scenes": [
            {"prompt": "Technical fixture scene", "negative_prompt": "", "duration_seconds": 1}
            for _ in range(2)
        ],
    }
    project = service.hybrid.create(read_topic(demo)["channels"][0]["id"], plan)
    # Последовательно обрабатываем элементы: range(2).
    for index in range(2):
        service.hybrid.import_scene(project["id"], index, f"scene{index}.mp4", plan["rights_basis"])
    result = service.hybrid.assemble(project["id"], 1)
    print(
        json.dumps(
            {
                "demo_root": str(demo),
                "result": result,
                "neural_generation": False,
                "external_posts": 0,
            },
            ensure_ascii=False,
        )
    )
finally:
    # Завершаем работу и освобождаем ресурсы независимо от результата.
    service.close()
