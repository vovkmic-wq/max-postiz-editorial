"""An explicitly enabled ESO policy enters the same guarded science route."""

from types import SimpleNamespace
import pytest
from max_postiz_agent.server_cycle import ServerCycle
from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError


def test_illustrated_eso_is_not_silently_skipped(monkeypatch):
    base = {"source_id": "nasa_svs_articles", "presentation_contract": "article-illustrated-v2"}
    eso = {**base, "source_id": "eso_potw"}
    article = {"url": "https://www.eso.org/public/images/potw2640a/"}
    calls = []

    def science(core, source_id, options, ledger, policy, **kw):
        calls.append(source_id)
        assert policy == eso
        return article

    monkeypatch.setattr("max_postiz_agent.server_science.select_science_article", science)
    cycle = SimpleNamespace(_policies=lambda: [base, eso], _policy_for_article=lambda a: eso)
    state = SimpleNamespace(assert_running=lambda: None, phase=lambda *a, **k: None)
    runner = ServerCycle(SimpleNamespace(), state=state, cycle=cycle, clock=lambda: 0)
    assert runner._select({"source": "open_science", "nasa": {}}, {"records": []}, base) == article
    assert calls == ["eso_potw"]


def test_eso_cycle_rejects_news_url_before_other_processing():
    cycle = HourlyArticleCycle.__new__(HourlyArticleCycle)
    policy = {
        "source_id": "eso_potw",
        "allowed_hosts": ["www.eso.org"],
        "rights_evidence": "https://www.eso.org/public/outreach/copyright/",
    }
    article = {
        "url": "https://www.eso.org/public/news/eso2640/",
        "title": "News",
        "original": "Not a picture caption",
        "published_at": 0,
    }
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
        cycle._article(article, policy, {"id": "eso_potw"}, None)
