"""Изоляция правил источников и отказ от универсального fallback."""

import pytest
from bs4 import BeautifulSoup

from max_postiz_agent.article_sources import source_spec
from max_postiz_agent.config import AgentError
from max_postiz_agent.server_science import _catalog_path
from max_postiz_agent.source_adapters import REGISTRY, adapter_for


def test_each_source_has_a_dedicated_module_and_independent_policy_copy():
    assert len({module.__name__ for module in REGISTRY.values()}) == len(REGISTRY)
    for name, module in REGISTRY.items():
        assert module.__name__.endswith("." + name)
        assert module.SPEC["id"] == name
        copied = source_spec(name)
        copied["rights_text_sha256"] = "changed"
        assert source_spec(name)["rights_text_sha256"] != "changed"


@pytest.mark.parametrize("unknown", ["science_022", "not_registered", None, []])
def test_catalog_entry_is_not_an_implicit_runtime_adapter(unknown):
    with pytest.raises(AgentError, match="EDITORIAL_SOURCE_POLICY_REQUIRED"):
        adapter_for(unknown)


def test_eso_notice_does_not_change_other_sources_or_allow_new_restrictions():
    literal = (
        '<a href="/public/outreach/ai-disclaimer/">This material is '
        "<strong>not</strong> AI generated</a>"
    )
    anchor = BeautifulSoup(literal, "html.parser").a
    assert adapter_for("eso_potw").known_notice(anchor)
    assert not adapter_for("noirlab_iotw").known_notice(anchor)
    assert not adapter_for("esahubble_potw").known_notice(anchor)
    anchor.append(" No derivatives")
    assert not adapter_for("eso_potw").known_notice(anchor)


def test_pagination_uses_each_sources_actual_route():
    assert _catalog_path("eso_potw", 1) == "/public/images/potw/"
    assert _catalog_path("eso_potw", 2) == "/public/images/potw/list/2/"
    assert _catalog_path("noirlab_iotw", 2) == "/public/images/iotw/page/2/"
    assert _catalog_path("esahubble_potw", 2) == "/images/potw/page/2/"
