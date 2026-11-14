"""Копия настоящего цикла не должна принимать исследовательские адаптеры как допуск."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select, func

from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_models import Dispatch, TargetApproval
from max_postiz_agent.source_adapters.candidates import CANDIDATES

pytest_plugins = ["test_article_cycle"]
ENTRIES = {
    item["id"]: item
    for item in json.loads(
        (Path(__file__).resolve().parents[1] / "config/editorial_source_catalog.json").read_text(
            encoding="utf-8"
        )
    )["sources"]
}


@pytest.mark.parametrize("source_id", sorted(CANDIDATES))
def test_candidate_cannot_enter_editorial_cycle_without_its_exact_policy(cycle, source_id):
    value, core, article, records, calls, _, _ = cycle
    article = {**article, "url": ENTRIES[source_id]["sample_url"]}
    with pytest.raises(AgentError) as rejected:
        value.prepare(article)
    assert rejected.value.code in {"EDITORIAL_SOURCE_POLICY_REQUIRED", "ARTICLE_SOURCE_URL_INVALID"}
    assert not calls and not records
    with core.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Dispatch)) == 0
        assert session.scalar(select(func.count()).select_from(TargetApproval)) == 0
