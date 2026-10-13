import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Base, Budget, Database, Job, now
from max_postiz_agent.editorial import validate_draft
from max_postiz_agent.service import Service


# Проверяем сценарий: postgresql schema compiles.
def test_postgresql_schema_compiles():
    # Последовательно обрабатываем элементы: Base.metadata.sorted_tables.
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=postgresql.dialect()))
    sql = str(select(Job).with_for_update(skip_locked=True).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql


# Проверяем сценарий: budget reservation serializes.
def test_budget_reservation_serializes(service):
    service.editor.mode = "live"
    service.editor.run = lambda task, context: ({"result": "ok"}, {})
    service.settings.app["opencode"]["max_task_cost_rub"] = 1
    service.settings.topic["channels"][0]["budget"]["daily_llm_limit"] = 1

    # Операция generate: данного модуля.
    def generate(_):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            service.model_task("bootstrap", "main", {})
            return "accepted"
        # Обрабатываем AgentError и выбираем допустимый исход.
        except AgentError as error:
            return error.code

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(generate, range(2)))
    assert sorted(results) == ["BUDGET_EXCEEDED", "accepted"]
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session() as session:
        assert sum(b.amount for b in session.scalars(select(Budget))) == 1


# Проверяем сценарий: worker claim only once.
def test_worker_claim_only_once(service):
    other_db = Database(service.settings)
    other = Service(service.settings, other_db)
    calls = []
    lock = threading.Lock()

    # Собираем материалы источника с учётом курсора и ограничений.
    def collect(source):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with lock:
            calls.append(source)
        time.sleep(0.1)

    service.collect = collect
    other.collect = collect
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as session:
        service.enqueue(session, "collect", "example_rss", now(), "concurrent-test")
        from max_postiz_agent.db import Cursor

        session.get(Cursor, "example_rss").next_due = now() + 10000
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker.tick) for worker in (service, other)]
        # Последовательно обрабатываем элементы: futures.
        for f in futures:
            f.result()
    assert calls == ["example_rss"]
    other.close()


# Проверяем сценарий: quality gate rejects invalid content.
@pytest.mark.parametrize(
    "variant,expected",
    [
        ("fake_number", "UNSUPPORTED_NUMBER"),
        ("missing_link", "MISSING_SOURCE_LINK"),
        ("wrong_language", "OUTPUT_NOT_RUSSIAN"),
        ("bad_quote", "INSUFFICIENT_EVIDENCE"),
        ("long_body", "LENGTH_EXCEEDED"),
    ],
)
def test_quality_gate_rejects_invalid_content(service, variant, expected):
    materials = [
        {
            "id": "m1",
            "title": "Видео",
            "text": "Видео длится 12 секунд.",
            "url": "https://example.org/a",
        }
    ]
    result = {
        "title": "Видео",
        "body": "Видео длится 12 секунд.\nhttps://example.org/a",
        "language": "ru",
        "source_ids": ["m1"],
        "decision": "accept",
        "reason_codes": [],
        "claims": [
            {
                "text": "Видео длится 12 секунд.",
                "material_id": "m1",
                "quote": "Видео длится 12 секунд.",
            }
        ],
        "confidence": 0.9,
        "payload": {},
    }
    # Обрабатываем ветвь при условии: variant == 'fake_number'.
    if variant == "fake_number":
        result["body"] += " 99"
    # Обрабатываем ветвь при условии: variant == 'missing_link'.
    if variant == "missing_link":
        result["body"] = "Видео длится 12 секунд."
    # Обрабатываем ветвь при условии: variant == 'wrong_language'.
    if variant == "wrong_language":
        result["language"] = "en"
    # Обрабатываем ветвь при условии: variant == 'bad_quote'.
    if variant == "bad_quote":
        result["claims"][0]["quote"] = "Вымышленный источник"
    # Обрабатываем ветвь при условии: variant == 'long_body'.
    if variant == "long_body":
        result["body"] += " видео" * 1000
    assert expected in validate_draft(
        result, materials, service.settings.channel("main"), model_verified=True
    )
