"""A count-only advisory protocol repair reuses a saved verdict, never a model."""
import copy
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from max_postiz_agent.article_quality import (
    MAX_REVIEW_WARNINGS,
    MAX_REVIEW_WARNINGS_BYTES,
    MEANING_FIRST_REVIEW_RUBRIC,
    REVIEW_PROTOCOL_CONTRACT,
    ArticleQuality,
    normalize_rubric_review,
    protocol_recovery_provenance,
    protocol_recovery_review,
    review_parser_contract,
)
from max_postiz_agent.config import AgentError, fingerprint
from test_article_quality import ORIGINAL, TITLE, TRANSLATION, URL

PASS = {"approved": True, "topic": "science", "issues": [],
        "warnings": ["Необязательное замечание " + letter for letter in "АБВГ"]}


class NoInference:
    def __init__(self, model):
        self.model, self.calls = model, 0

    def generate_text(self, _):
        self.calls += 1
        pytest.fail("Protocol recovery must not sample any model")


def quality(*, matches=None, status=200, payload=None, trace=None, rubric=MEANING_FIRST_REVIEW_RUBRIC):
    translator, reviewer, events, checked_texts = NoInference("translator"), NoInference("reviewer"), [], []

    def spelling(request):
        checked_texts.append(parse_qs(request.content.decode())["text"][0])
        return httpx.Response(status, json={"matches": matches or []} if payload is None else payload)

    check = ArticleQuality(translator, reviewer, review_rubric=rubric,
        trace=events.append if trace is None else trace, transport=httpx.MockTransport(spelling))
    return check, translator, reviewer, events, checked_texts


def recover(check, raw=None, **changes):
    args = {"title": TRANSLATION["title"], "body": TRANSLATION["body"],
            "original_title": TITLE, "original": ORIGINAL, "url": URL,
            "raw_review": json.dumps(PASS, ensure_ascii=False) if raw is None else raw,
            "expected_review_prompt_hash": check.review_prompt_hash, **changes}
    return check.recover_review_protocol(**args)


@pytest.mark.parametrize("count", range(4, 9))
def test_saved_positive_overflow_preserves_every_warning_and_has_fresh_spelling_without_inference(count):
    review = {**PASS, "warnings": ["Совет " + str(i) for i in range(count)]}
    raw = "```json\n" + json.dumps(review, ensure_ascii=False, indent=2) + "\n```"
    original_raw = raw
    check, translator, reviewer, events, checked_texts = quality()
    try:
        result = recover(check, raw)
    finally:
        check.close()
    text = TRANSLATION["title"] + "\n\n" + TRANSLATION["body"]
    assert translator.calls == reviewer.calls == 0 and checked_texts == [text]
    assert raw == original_raw and result["warnings"] == review["warnings"]
    assert result["machine_review_status"] == "passed" and result["review_rubric"] == MEANING_FIRST_REVIEW_RUBRIC
    evidence = result["machine_review_evidence"]
    assert evidence["review_raw"] == raw and evidence["review"] == review
    assert evidence["hard_gates_passed"] is True and evidence["spelling_unresolved"] == []
    assert evidence["source_hash"] == fingerprint({"title": TITLE, "body": ORIGINAL, "url": URL})
    assert evidence["text_hash"] == fingerprint({"title": result["title"], "body": result["body"]})
    protocol = result["review_protocol_recovery"]
    assert protocol["contract"] == REVIEW_PROTOCOL_CONTRACT and protocol["legacy_warning_limit"] == 3
    assert protocol["warning_count"] == count and protocol["raw_review_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert protocol["review_prompt_hash"] == check.review_prompt_hash
    assert evidence["parser_contract"] == review_parser_contract(raw)
    assert evidence["protocol_recovery"] == protocol == protocol_recovery_provenance(raw, check.review_prompt_hash)
    assert evidence["parser_contract"]["contract_hash"] == protocol["parser_contract_hash"]
    assert events[0]["phase"] == "protocol_recovery_started" and events[0]["raw"] == raw
    assert events[1]["phase"] == "protocol_recovery_validated" and events[1]["review"] == review
    assert [event["phase"] for event in events][-4:] == [
        "spelling_started", "spelling_returned", "spelling_adjudicated", "accepted"]
    assert not any(event["phase"] in {"translation_started", "review_started"} for event in events)


@pytest.mark.parametrize("bad", [
    {**PASS, "approved": False}, {**PASS, "approved": "true"}, {**PASS, "approved": 1},
    {**PASS, "issues": ["A substantive mismatch remains."]}, {**PASS, "topic": "other"},
    {**PASS, "topic": "Science"}, {**PASS, "topic": None},
    {**PASS, "warnings": ["x"] * 3}, {**PASS, "warnings": ["x"] * 9},
    {**PASS, "warnings": "four warnings"}, {**PASS, "warnings": ["x", "x", "x", ""]},
    {**PASS, "warnings": ["x", "x", "x", {"issue": "not a string"}]},
    {**PASS, "warnings": ["x" * 1025] * 4},
    {**PASS, "warnings": ["я" * 513] * 8},
    {**PASS, "warnings": ["x", "x", "x", "\ud800"]},
    {**PASS, "extra": "cannot repair an unknown contract"},
    {key: value for key, value in PASS.items() if key != "issues"},
])
def test_malformed_or_nonpositive_raw_is_blocked_before_language_tool(bad):
    check, translator, reviewer, events, spelling = quality()
    try:
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"):
            recover(check, json.dumps(bad))
    finally:
        check.close()
    assert not spelling and not events and translator.calls == reviewer.calls == 0


@pytest.mark.parametrize("raw", [
    '{"approved":false,"approved":true,"topic":"science","issues":[],"warnings":["a","b","c","d"]}',
    json.dumps(PASS) + "\n" + json.dumps(PASS), "not JSON", "[]", "null",
    " " * 65536 + json.dumps(PASS),
], ids=["duplicate_keys", "two_objects", "not_json", "array", "null", "oversized"])
def test_ambiguous_duplicate_or_oversized_complete_payloads_cannot_be_recovered(raw):
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"):
        protocol_recovery_review(raw)


def test_warning_count_and_utf8_byte_bounds_are_independent_and_do_not_modify_negative_verdicts():
    accepted = {**PASS, "warnings": ["x" * 1024] * MAX_REVIEW_WARNINGS}
    assert sum(len(v.encode()) for v in accepted["warnings"]) == MAX_REVIEW_WARNINGS_BYTES
    assert normalize_rubric_review(accepted, MEANING_FIRST_REVIEW_RUBRIC) == accepted
    rejected = {**accepted, "approved": False, "issues": ["A real semantic problem."]}
    before = copy.deepcopy(rejected)
    normalized = normalize_rubric_review(rejected, MEANING_FIRST_REVIEW_RUBRIC)
    assert normalized == rejected == before and normalized["approved"] is False
    normalized["warnings"].clear()
    assert rejected == before
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"):
        protocol_recovery_review(json.dumps(rejected))
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
        normalize_rubric_review({**rejected, "issues": ["Issue"] * 4}, MEANING_FIRST_REVIEW_RUBRIC)


@pytest.mark.parametrize("changes,error", [
    ({"body": TRANSLATION["body"] + " В 2049 году."}, "TRANSLATION_NUMBER_CHANGED"),
    ({"body": ("This is a complete English sentence about 3 hours in the atmosphere and unusual stellar winds. " * 2).strip()},
     "TRANSLATION_NOT_RUSSIAN"),
    ({"original": ORIGINAL + " See https://example.org/source/"}, "TRANSLATION_REFERENCE_CHANGED"),
    ({"body": TRANSLATION["body"] * 70}, "ARTICLE_POST_TOO_LONG"),
])
def test_every_hard_gate_runs_again_and_blocks_before_lt(changes, error):
    check, translator, reviewer, events, spelling = quality()
    try:
        with pytest.raises(AgentError, match=error):
            recover(check, **changes)
    finally:
        check.close()
    assert not spelling and translator.calls == reviewer.calls == 0
    assert events[-2]["phase"] == "gate_rejected" and events[-1]["phase"] == "rejected"
    assert not any(event["phase"] == "accepted" for event in events)


@pytest.mark.parametrize("changes", [
    {"title": " " + TRANSLATION["title"]},
    {"body": TRANSLATION["body"] + "\n"},
    {"body": TRANSLATION["body"] + " [Ссылка](https://example.org/reference/)"},
    {"original": " " + ORIGINAL}, {"url": URL + "#fragment"},
    {"expected_review_prompt_hash": "0" * 64},
])
def test_recovery_never_reformats_bound_inputs_or_changes_the_semantic_prompt(changes):
    check, _, _, events, spelling = quality()
    try:
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"):
            recover(check, **changes)
    finally:
        check.close()
    assert not events and not spelling


@pytest.mark.parametrize("matches,status,payload,error", [
    ([{"rule": {"id": "REAL_GRAMMAR", "issueType": "grammar"}}], 200, None, "ARTICLE_QUALITY_REJECTED"),
    ([{}], 200, None, "ARTICLE_QUALITY_REJECTED"),
    (None, 503, None, "ARTICLE_SPELLING_UNAVAILABLE"),
    (None, 200, {"matches": "invalid"}, "ARTICLE_SPELLING_UNAVAILABLE"),
    (None, 200, {}, "ARTICLE_SPELLING_UNAVAILABLE"),
])
def test_fresh_lt_faults_unknown_matches_and_infrastructure_remain_blocking(matches, status, payload, error):
    check, translator, reviewer, events, spelling = quality(matches=matches, status=status, payload=payload)
    try:
        with pytest.raises(AgentError, match=error):
            recover(check)
    finally:
        check.close()
    assert len(spelling) == 1 and translator.calls == reviewer.calls == 0
    assert not any(event["phase"] == "accepted" for event in events)


def test_legacy_rubric_and_failed_trace_cannot_use_recovery():
    legacy, _, _, _, spelling = quality(rubric="legacy")
    try:
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"):
            recover(legacy)
    finally:
        legacy.close()
    assert not spelling
    check, _, _, _, spelling = quality(trace=lambda _: (_ for _ in ()).throw(OSError("trace write failed")))
    try:
        with pytest.raises(AgentError, match="ARTICLE_TRACE_FAILED"):
            recover(check)
    finally:
        check.close()
    assert not spelling
