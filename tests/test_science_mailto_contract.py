"""Два буквальных mailto только для новых иллюстрированных NOIRLab-статей."""
import json

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.server_nasa import SCIENCE_MAILTO_CONTRACT, SCIENCE_MAILTO_HREFS, _parser
from max_postiz_agent.server_science import select_science_article
from test_server_science import STAMP

pytest_plugins = ["test_server_science"]
BASE = "https://noirlab.edu/public/images/iotw2640a/"


@pytest.mark.parametrize("href", sorted(SCIENCE_MAILTO_HREFS))
def test_new_scoped_extractor_preserves_literal_mailto_and_whole_paragraph(href):
    parser = _parser(science_mailto=True)
    assert parser._narrative_href(href, BASE) == href
    label = href.removeprefix("mailto:")
    paragraph = f'<p>The complete final paragraph stays. Contact <a href="{href}">{label}</a> for details.</p>'
    assert parser.narrative_text(paragraph, BASE) == (
        f"The complete final paragraph stays. Contact [{label}]({href}) for details.")
    assert parser.science_extraction_contract == SCIENCE_MAILTO_CONTRACT
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_INVALID"):
        _parser()._narrative_href(href, BASE)


@pytest.mark.parametrize("href", [
    "mailto:foreign@example.org", "MAILTO:comms@noirlab.edu", "mailto:Comms@noirlab.edu",
    "mailto:comms@noirlab.edu?subject=Hi", "mailto:comms@noirlab.edu?bcc=other@example.org",
    "mailto:comms@noirlab.edu,info@noirlab.edu", "mailto:comms%40noirlab.edu",
    "mailto:comms@noirlab.edu%0D%0ABcc:other@example.org", "mailto:comms@noirlab.edu\r\n",
    "mailto:comms@noirlab.edu#fragment", " mailto:comms@noirlab.edu", "javascript:alert(1)",
    "data:text/html,mail", "mailto://comms@noirlab.edu", "mailto:info@noirlab.edu/",
])
def test_new_extractor_rejects_injected_or_nonliteral_mailto(href):
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_INVALID"):
        _parser(science_mailto=True)._narrative_href(href, BASE)


@pytest.mark.parametrize("base", [
    "https://esahubble.org/images/potw2640a/", "https://svs.gsfc.nasa.gov/12345/",
    "https://noirlab.edu/public/images/iotw/", "http://noirlab.edu/public/images/iotw2640a/",
    "https://noirlab.edu/public/images/iotw2640a/?query=1", "https://noirlab.edu/public/images/iotw2640a/#x",
    "https://noirlab.edu.evil.test/public/images/iotw2640a/", "https://NOIRLAB.EDU/public/images/iotw2640a/",
])
def test_mailto_is_not_enabled_for_another_source_or_noncanonical_base(base):
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_INVALID"):
        _parser(science_mailto=True)._narrative_href("mailto:comms@noirlab.edu", base)


def test_new_parser_instance_does_not_change_legacy_extractor_or_regular_links():
    old = _parser()
    new = _parser(science_mailto=True)
    href = "https://example.org/exact?x=1#part"
    assert old._narrative_href(href, BASE) == new._narrative_href(href, BASE) == href
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_LINK_INVALID"):
        old._narrative_href("mailto:info@noirlab.edu", BASE)
    with pytest.raises(AgentError, match="SCIENCE_EXTRACTION_CONTRACT_INVALID"):
        _parser(science_mailto="yes")


def test_selection_enables_scoped_mailto_only_for_new_presentation_and_preserves_proof(source):
    if source.source_id != "noirlab_iotw":
        # The parametrized ESA source proves that its identical mailto is still forbidden.
        expected = None
    else:
        expected = "mailto:comms@noirlab.edu"
    body = ('<p>Researchers preserve the complete scientific narrative and its facts. '
            'They compare the galaxy with previous measurements. '
            'Contact <a href="mailto:comms@noirlab.edu">comms@noirlab.edu</a> for details.</p>')
    calls = []
    def response(html):
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    def request(request):
        calls.append(str(request.url))
        if str(request.url) == source.url:
            return response(source.detail(body=body))
        if str(request.url) == source.spec["rights_evidence"]:
            from urllib.parse import urlsplit
            return response(source.pages[urlsplit(source.spec["rights_evidence"]).path])
        return response(source.catalog())
    policy = {**source.policy, "presentation_contract": "article-illustrated-v1"}
    article = select_science_article(source.core, source.source_id, source.options, {"records": []}, policy,
        now=STAMP, checkpoint=False, transport=httpx.MockTransport(request))
    if expected is None:
        assert article is None
    else:
        assert expected in article["original"] and "for details." in article["original"]
        proofs = list((source.core.settings.root / "data/server-sources").glob(source.source_id + "-*.local.json"))
        proof = json.loads(proofs[0].read_text(encoding="utf-8"))
        assert proof["extraction_contract"] == SCIENCE_MAILTO_CONTRACT
        assert proof["article"] == article
    assert len(calls) == len(set(calls))


def test_new_http_only_selection_reuses_exact_saved_legacy_proof_without_rewriting_it(source):
    old_article = source.run()
    path = next((source.root / "data/server-sources").glob(source.source_id + "-*.local.json"))
    raw = path.read_bytes()
    assert "extraction_contract" not in json.loads(raw)
    article = select_science_article(source.core, source.source_id, source.options, {"records": []},
        {**source.policy, "presentation_contract": "article-illustrated-v1"}, now=STAMP,
        checkpoint=False, transport=httpx.MockTransport(source.handler))
    assert article == old_article
    assert path.read_bytes() == raw
