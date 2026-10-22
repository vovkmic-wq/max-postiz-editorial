"""Полный неверный ответ изолируется; неизвестный запрос не закрывается автоматически."""
import json

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.server_cycle import ServerCycle
from max_postiz_agent.server_state import ServerState

pytest_plugins = ["test_automatic_article_repair"]


def record(value):
    return value.cycle._load()["records"][0]


def quarantine(value):
    value.controls.output = "not json"
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_RESPONSE_INVALID"):
        value.cycle.repair_rejected(value.initial["id"])
    assert record(value)["automatic_repair_attempts"][0]["outcome_origin"] == "protocol_guard"


def test_complete_invalid_response_closes_next_hour_without_freeing_source_or_repeating_models(repair_cycle):
    value = repair_cycle
    quarantine(value)
    value.stamp[0] += 3600
    store = ServerState(value.core)
    store.initialize()
    store.control("start", 1, "owner")
    controller = ServerCycle(value.core, state=store, cycle=value.cycle, clock=lambda: value.stamp[0])
    assert controller._close_known_rejection(record(value)) is True
    saved = record(value)
    assert saved["state"] == "cancelled" and saved["slot"] == value.initial["slot"]
    assert saved["url"] == value.article["url"] and saved["original_hash"] == value.initial["original_hash"]
    assert value.trace.read_bytes() == value.initial_trace
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert not value.records and not value.writes


@pytest.mark.parametrize("damage", ["raw", "time", "binding", "audit"])
def test_quarantine_proof_damage_blocks_expired_auto_close(repair_cycle, damage):
    from sqlalchemy import select
    from max_postiz_agent.db import Audit
    value = repair_cycle
    quarantine(value)
    claim = record(value)["automatic_repair_attempts"][0]
    if damage == "audit":
        with value.core.db.session.begin() as session:
            audit = session.scalar(select(Audit).where(Audit.action == "automatic_article_repair_quarantined"))
            session.delete(audit)
    else:
        path = value.core.settings.root / claim["repair_trace"]
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if damage == "raw":
            events[1]["raw"] += " changed"
        elif damage == "time":
            events[1]["at"] = True
        else:
            events[1]["binding"]["input_hash"] = "changed"
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(record(value))["reason"] == "TRACE_INVALID"
    controller = ServerCycle(value.core, cycle=value.cycle, clock=lambda: value.stamp[0])
    assert controller._close_known_rejection(record(value)) is False
    assert value.counts["qwen"] == 1 and not value.records


def test_unknown_editor_network_failure_is_never_quarantined(repair_cycle):
    value = repair_cycle
    value.controls.output = AgentError("OPENCODE_GENERATION_FAILED", 503)
    with pytest.raises(AgentError, match="OPENCODE_GENERATION_FAILED"):
        value.cycle.repair_rejected(value.initial["id"])
    value.stamp[0] += 3600
    saved = record(value)
    assert saved["automatic_repair_attempts"][0]["state"] == "failed"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "FAILED"
    assert value.counts["qwen"] == 1 and not value.records
