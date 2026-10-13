"""Repeatable offline demonstration in data/demo; no external source or MAX requests."""

import json
import shutil
from pathlib import Path

from sqlalchemy import select

from max_postiz_agent.cli import build_service, initialize
from max_postiz_agent.collectors import Item
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Draft, Story, now

root = Path(__file__).resolve().parents[1]
demo = root / "data/demo"
demo.mkdir(parents=True, exist_ok=True)
# Последовательно обрабатываем элементы: ('schemas', 'prompts').
for folder in ("schemas", "prompts"):
    shutil.copytree(root / folder, demo / folder, dirs_exist_ok=True)
(demo / "config").mkdir(exist_ok=True)
shutil.copyfile(root / "TOPIC.md", demo / "TOPIC.md")
app = json.loads((root / "config/app.example.json").read_text(encoding="utf-8-sig"))
app["opencode"]["adapter_mode"] = "fake"
(demo / "config/app.json").write_text(json.dumps(app))
sources = json.loads((root / "config/sources.example.json").read_text(encoding="utf-8-sig"))
source = sources["sources"][0]
source["enabled"] = True
source["rights"].update(
    verified=True,
    allow_storage=True,
    allow_derivative=True,
    evidence_ref="Generated offline fixture",
)
(demo / "config/sources.json").write_text(json.dumps(sources))
initialize(demo)
service = build_service(demo)
# Выполняем операцию с отдельной обработкой возможных ошибок.
try:
    service.ingest(
        "example_rss",
        [
            Item(
                "demo-video",
                "Как создаётся нейровидео",
                "Техническая фикстура: короткое видео длительностью 12 секунд.",
                "https://example.org/demo",
                now(),
                "ru",
            )
        ],
    )
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        story = session.scalar(select(Story))
        story_id = story.id
    did = service.edit_story("main", story_id)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        draft = session.get(Draft, did)
        print(
            json.dumps(
                {
                    "demo_root": str(demo),
                    "draft_id": did,
                    "state": draft.state,
                    "body": draft.current["body"],
                    "content_hash": fingerprint(draft.current),
                    "external_posts": 0,
                    "model_generated": False,
                },
                ensure_ascii=False,
            )
        )
finally:
    # Завершаем работу и освобождаем ресурсы независимо от результата.
    service.close()
