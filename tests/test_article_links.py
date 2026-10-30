"""Новая диагностика сохраняет полный href и не переписывает историю v1."""

import copy
import re

import pytest

from max_postiz_agent.article_links import (
    LEGACY_REFERENCE_CONTRACT,
    REFERENCE_CONTRACT,
    REFERENCE_CONTRACTS,
    reference_urls,
)
from max_postiz_agent.article_quality import article_reference_urls
from max_postiz_agent.config import AgentError

URL = "https://en.wikipedia.org/wiki/Asterism_(astronomy)"


def v2(text):
    return reference_urls(text, contract=REFERENCE_CONTRACT)


@pytest.mark.parametrize("text", [
    "", "Text without references.", "https://example.org/one.,;",
    URL, URL[:-1], URL + "/wrong", "[asterism](" + URL + ").",
    "https://example.org/a?b=1#c; https://example.org/two",
    "HTTPS://example.org/UPPER", "https://example.org/x]tail",
    "https://example.org/(x(y)) https://example.org/%28x%29",
    "\"https://example.org/quoted\" <https://example.org/angle>",
])
def test_v1_is_exactly_the_original_extractor_without_changing_legacy_semantics(text):
    expected = {value.rstrip(".,;") for value in re.findall(r"https?://[^\s<>\"\]\)]+", text)}
    assert reference_urls(text, contract=LEGACY_REFERENCE_CONTRACT) == expected == article_reference_urls(text)


def test_contract_selection_is_explicit_and_supported_contracts_are_fixed():
    assert LEGACY_REFERENCE_CONTRACT == "article-links-v1"
    assert REFERENCE_CONTRACT == "article-links-v2"
    assert REFERENCE_CONTRACTS == frozenset({"article-links-v1", "article-links-v2"})
    with pytest.raises(TypeError):
        reference_urls(URL)


@pytest.mark.parametrize("contract", [None, False, [], {}, "article-links-v0", "article-links-v3"])
def test_unknown_contract_is_rejected_without_guessing_a_parser(contract):
    with pytest.raises(AgentError, match="ARTICLE_REFERENCE_CONTRACT_INVALID"):
        reference_urls(URL, contract=contract)


@pytest.mark.parametrize("text", [
    URL, "[asterism](" + URL + ")", "[asterism](" + URL + ").",
    "([asterism](" + URL + ")).", "An asterism (" + URL + ").",
    "An asterism ((" + URL + ")); more text.",
    '<' + URL + '>', '"' + URL + '"',
])
def test_full_balanced_bare_markdown_and_proven_wrappers_keep_the_complete_href(text):
    assert v2(text) == {URL}


@pytest.mark.parametrize("raw", [
    URL[:-1], URL + "/wrong", URL + ")", URL + ")/wrong",
    "https://example.org/(nested(unclosed)",
])
def test_malformed_or_changed_bare_tokens_remain_diagnostic_and_differ_from_the_full_source(raw):
    assert v2(raw) == {raw}
    assert v2(raw) != v2("[asterism](" + URL + ")")


@pytest.mark.parametrize("raw,expected", [
    ("[asterism](" + URL[:-1], URL[:-1]),
    ("[asterism](" + URL, URL),
    ("[asterism](" + URL + "/wrong)", URL + "/wrong"),
    ("[asterism](" + URL[:-1] + " more text", URL[:-1]),
])
def test_incomplete_markdown_never_prevents_building_a_diagnostic_manifest(raw, expected):
    assert v2(raw) == {expected}


@pytest.mark.parametrize("url", [
    "https://example.org/a_(b(c))?x=(d(e))#part_(f)",
    "https://example.org/%28a%29/%2f?x=%28&x=%29#frag%2F",
    "https://example.org/a?x=1&next=https://other.example.org/x#part",
    "https://example.org/a?x=a,b;#c,;.",
    "http://EXAMPLE.org/Case?Repeated=01&Repeated=02#ExactFragment",
    "HTTPS://EXAMPLE.org/Case_(A)?q=(B)#(C)",
])
def test_balanced_href_query_fragment_encoding_and_case_are_literal_without_normalization(url):
    assert v2(url) == {url}
    assert v2("[данные](" + url + ").") == {url}


@pytest.mark.parametrize("suffix", [".", ",", ";", ".,;", "/wrong", "?changed=1", "#changed"])
def test_ambiguous_bare_punctuation_and_changed_suffix_are_not_silently_trimmed(suffix):
    assert v2(URL + suffix) == {URL + suffix}
    assert v2(URL + suffix) != {URL}


@pytest.mark.parametrize("url", ["https://example.org/path.", "https://example.org/path,",
                                "https://example.org/path;", "https://example.org/a?x=a,b;"])
def test_punctuation_inside_a_known_markdown_destination_is_part_of_the_literal_href(url):
    assert v2("[данные](" + url + ").,;") == {url}


def test_deduplication_and_manifest_set_difference_preserve_missing_and_unexpected_urls():
    other = "https://example.org/other_(part)?version=03#fig2"
    source = "[asterism](" + URL + ") [other](" + other + ") " + URL
    current = URL[:-1] + " " + other + " " + other
    before = copy.deepcopy((source, current))
    required, observed = v2(source), v2(current)
    assert required == {URL, other}
    assert observed == {URL[:-1], other}
    assert required - observed == {URL} and observed - required == {URL[:-1]}
    assert (source, current) == before


def test_embedded_query_url_is_one_literal_reference_and_newline_separates_actual_references():
    url = "https://example.org/?next=https://other.example.org/a_(b)#c"
    assert v2(url) == {url}
    assert v2(url + "\n" + URL) == {url, URL}


def test_extractor_is_tolerant_not_a_security_validator_or_url_repairer():
    raw = "http://user:password@127.0.0.1:invalid/(unfinished"
    assert v2(raw) == {raw}
    assert "http://user:password@127.0.0.1:invalid/(unfinished" in v2(raw)
