"""Historical refusals remain exact evidence, rather than a fresh model sample."""

import copy
import hashlib
import json

import httpx
import pytest

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.reddit import text_hash
from max_postiz_agent.rejected_trace import ERROR, recover_rejected

IDENT = "8fad313d-200d-456e-9a62-2aba964c0071"
ARTICLE = {"url": "https://svs.gsfc.nasa.gov/5591/", "title": "A classroom experiment",
           "original": "During a classroom experiment, a sealed glass jar stood on a wooden table. "
                       "The lamp warmed the jar slowly, and a sensor recorded the change in temperature.",
           "published_at": 100.0}
CANDIDATE = {"title": "Учебный опыт", "body":
             "Во время учебного опыта герметично закрытая стеклянная банка стояла на деревянном столе. "
             "Лампа медленно нагревала банку, а датчик фиксировал изменение температуры."}
REVIEW = {"approved": False, "topic": "science", "issues": ["Сохранённое замечание независимой модели."]}


class UnusedTranslator:
    model = "hy-mt2-7b-q8"

    def generate_text(self, _):
        pytest.fail("Historical recovery must not translate")


class Reviewer:
    model = "ministral"

    def __init__(self, review):
        self.review = review

    def generate_text(self, _):
        return json.dumps(self.review, ensure_ascii=False), {}


def encode(events):
    return ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode()


def fixture(review=None, matches=None):
    events = []

    def trace(event):
        events.append({**event, "at": 1001.0 + len(events), "record_id": IDENT})

    quality = ArticleQuality(UnusedTranslator(), Reviewer(review or REVIEW), trace=trace,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": matches or []})))
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            quality.check_candidate(CANDIDATE["title"], CANDIDATE["body"],
                                    ARTICLE["title"], ARTICLE["original"], ARTICLE["url"])
    finally:
        quality.close()
    record = {"id": IDENT, "url": ARTICLE["url"], "claimed_at": 800.0,
              "state": "cancelled", "cancelled_at": 1200.0,
              "source_published_at": ARTICLE["published_at"],
              "input_hash": fingerprint(ARTICLE), "original_hash": text_hash(ARTICLE["original"]),
              "preparation_attempts": [{"number": 1, "started_at": 1000.0, "finished_at": 1100.0,
                  "operator_correction": True, "state": "needs_attention",
                  "error": "ARTICLE_QUALITY_REJECTED", "candidate_hash": fingerprint(CANDIDATE),
                  "candidate_raw_hash": fingerprint(CANDIDATE)}]}
    return record, copy.deepcopy(ARTICLE), events


def invalid(record, article, events):
    with pytest.raises(AgentError, match=ERROR) as caught:
        recover_rejected(record, article, encode(events))
    assert caught.value.status == 409


def test_recovers_actual_candidate_trace_without_network_or_mutating_history(monkeypatch):
    record, article, events = fixture()
    raw = encode(events)
    before = copy.deepcopy((record, article))
    monkeypatch.setattr(httpx.Client, "request", lambda *args, **kwargs: pytest.fail("No network"))
    recovered = recover_rejected(record, article, raw)
    assert (record, article) == before
    assert recovered["machine_review_status"] == "rejected"
    assert recovered["translation"] == CANDIDATE["title"] + "\n\n" + CANDIDATE["body"]
    assert recovered["body"].endswith("Источник: " + article["url"]
                                       + "\nПеревод и редактура с помощью нейросетей.")
    proof = recovered["machine_review_evidence"]
    assert proof["review"] == REVIEW and proof["spelling_unresolved"] == []
    assert proof["hard_gates_passed"] is True
    assert proof["trace_hash"] == hashlib.sha256(raw).hexdigest()
    assert proof["segment_hash"] == fingerprint(events)
    assert proof["text_hash"] == fingerprint({"title": recovered["title"], "body": recovered["body"]})
    assert proof["source_hash"] == fingerprint({"title": article["title"],
                                               "body": article["original"], "url": article["url"]})


def test_final_outer_attempt_selects_only_its_own_inner_attempt_one():
    record, article, events = fixture()
    earlier = copy.deepcopy(events)
    for event in earlier:
        event["at"] -= 200
    record["preparation_attempts"].insert(0, {"number": 1, "started_at": 800.0,
        "finished_at": 900.0, "error": "ARTICLE_QUALITY_REJECTED"})
    record["preparation_attempts"][-1]["number"] = 2
    result = recover_rejected(record, article, encode(earlier + events))
    assert result["machine_review_evidence"]["segment_hash"] == fingerprint(events)


@pytest.mark.parametrize("change", [
    lambda r, a, e: a.update(original=a["original"] + " Another finding."),
    lambda r, a, e: a.update(title="A different source"),
    lambda r, a, e: a.update(url="https://svs.gsfc.nasa.gov/5592/"),
    lambda r, a, e: a.update(published_at=101.0),
    lambda r, a, e: r.update(input_hash="0" * 64),
    lambda r, a, e: r.update(original_hash="0" * 64),
    lambda r, a, e: r["preparation_attempts"][-1].update(operator_correction=False),
    lambda r, a, e: r["preparation_attempts"][-1].update(state="preparing"),
    lambda r, a, e: r["preparation_attempts"][-1].update(error="ARTICLE_SPELLING_UNAVAILABLE"),
    lambda r, a, e: r["preparation_attempts"][-1].update(candidate_hash="0" * 64),
    lambda r, a, e: r["preparation_attempts"][-1].update(candidate_raw_hash="0" * 64),
    lambda r, a, e: r["preparation_attempts"][-1].pop("finished_at"),
    lambda r, a, e: r["preparation_attempts"][-1].update(number=2),
    lambda r, a, e: e[0]["candidate"].update(body=CANDIDATE["body"] + " Подмена."),
    lambda r, a, e: e[0]["source_untrusted"].update(body="Different original"),
    lambda r, a, e: e[2]["context"].update(translation_untrusted="Different translation"),
    lambda r, a, e: e[2]["context"].update(source_untrusted="Different original"),
    lambda r, a, e: e[-1]["feedback"].update(review_issues=[]),
    lambda r, a, e: e[0].update(record_id="0" * 36),
    lambda r, a, e: e[0].update(at=float("nan")),
    lambda r, a, e: e[-1].update(at=1200.0),
    lambda r, a, e: e[1].update(at=999.0),
    lambda r, a, e: e.append({**e[-1], "phase": "candidate_started", "at": 1200.0}),
    lambda r, a, e: e.insert(1, copy.deepcopy(e[0])),
    lambda r, a, e: e.pop(-1),
    lambda r, a, e: e[-1].update(error="INVALID_EDITOR_RESULT"),
])
def test_rejects_source_candidate_attempt_and_segment_tampering(change):
    record, article, events = fixture()
    change(record, article, events)
    invalid(record, article, events)


@pytest.mark.parametrize("review", [
    {**REVIEW, "approved": "false"}, {**REVIEW, "approved": True},
    {**REVIEW, "issues": []}, {**REVIEW, "issues": ["one"] * 4},
    {**REVIEW, "issues": ["x" * 1025]}, {**REVIEW, "topic": "politics"},
    {**REVIEW, "extra": "unvalidated verdict"},
])
def test_no_invalid_positive_or_disallowed_topic_can_become_a_recovered_refusal(review):
    record, article, events = fixture(review=review)
    invalid(record, article, events)


def test_raw_review_must_match_parsed_verdict():
    record, article, events = fixture()
    next(e for e in events if e["phase"] == "review_returned")["raw"] = json.dumps(
        {"approved": True, "topic": "science", "issues": []})
    invalid(record, article, events)


def test_normalized_issue_envelope_preserves_complete_raw_and_all_three_findings():
    issues = ["Первое замечание. " * 40, "Второе замечание. " * 40, "Третье замечание. " * 40]
    record, article, events = fixture(review={**REVIEW, "issues": [{"issue": x} for x in issues]})
    recovered = recover_rejected(record, article, encode(events))
    evidence = recovered["machine_review_evidence"]
    assert evidence["review"]["issues"] == issues
    assert json.loads(evidence["review_raw"])["issues"] == [{"issue": x} for x in issues]


def test_lt_unresolved_cannot_be_hidden_by_a_forged_adjudication():
    record, article, events = fixture(matches=[{"message": "Real spelling issue"}])
    next(e for e in events if e["phase"] == "spelling_adjudicated")["unresolved"] = []
    events[-1]["feedback"]["spelling_matches"] = []
    invalid(record, article, events)


@pytest.mark.parametrize("body", [CANDIDATE["body"] + " 2 наблюдения.",
                                  ARTICLE["original"], "Слишком длинный русский текст. " * 150])
def test_hard_gates_are_recomputed_even_when_historical_gate_pass_is_forged(body):
    record, article, events = fixture()
    candidate = {**CANDIDATE, "body": body}
    events[0]["candidate"] = candidate
    record["preparation_attempts"][-1].update(candidate_hash=fingerprint(candidate),
                                             candidate_raw_hash=fingerprint(candidate))
    text = candidate["title"] + "\n\n" + body
    events[1]["characters"] = len(text + "\n\nИсточник: " + article["url"]
                                   + "\nПеревод и редактура с помощью нейросетей.")
    events[2]["context"]["translation_untrusted"] = text
    invalid(record, article, events)


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"{}\n", b"\n", b"x" * (2 * 1024 * 1024 + 1)],
                         ids=["not-json", "invalid-utf8", "missing-event", "blank", "oversized"])
def test_invalid_or_oversized_trace_is_a_safe_error(raw):
    record, article, _ = fixture()
    with pytest.raises(AgentError, match=ERROR):
        recover_rejected(record, article, raw)


def test_duplicate_json_keys_are_ambiguous():
    record, article, events = fixture()
    raw = encode(events).replace(b'"phase": "candidate_started"',
                                b'"phase": "translation_started", "phase": "candidate_started"', 1)
    with pytest.raises(AgentError, match=ERROR):
        recover_rejected(record, article, raw)


def test_duplicate_review_keys_cannot_be_hidden_in_a_valid_trace_envelope():
    record, article, events = fixture()
    review = next(e for e in events if e["phase"] == "review_returned")
    review["raw"] = review["raw"].replace('"approved": false',
                                         '"approved": true, "approved": false')
    invalid(record, article, events)
