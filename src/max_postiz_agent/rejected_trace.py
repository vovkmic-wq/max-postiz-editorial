"""Recover one completed historical refusal without invoking a model or granting approval.

The caller still owns policy, rights, age, duplicate, database and remote-state
checks. This parser only establishes exact source/candidate/machine evidence.
"""

import copy
import hashlib
import math
import re

from .adapters import extract_object
from .article_quality import (
    TOPICS,
    _normalize_review,
    _validated_source,
    adjudicate_scientific_spelling,
    canonical_article_url,
    normalize_article_links,
)
from .config import AgentError, fingerprint, parse_json
from .editorial import translation_gate
from .reddit import text_hash

ERROR = "ARTICLE_REJECTED_EVIDENCE_INVALID"
MAX_TRACE_BYTES = 2 * 1024 * 1024


def _require(value):
    if not value:
        raise AgentError(ERROR, 409)


def _stamp(value):
    _require(type(value) in {int, float} and math.isfinite(value) and value >= 0)
    return value


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))
    return value


def recover_rejected(record, article, raw: bytes):
    """Return a draft payload with a retained negative verdict, never a machine pass.

    Historical traces restart their inner attempt counter for every invocation.
    Only the final ledger attempt's timestamp interval and operator candidate
    hashes can identify a segment; ambiguity is an error, not a recovery guess.
    """
    try:
        return _recover(record, article, raw)
    except (AgentError, ValueError, TypeError, KeyError, UnicodeError, OverflowError,
            RecursionError) as exc:
        raise AgentError(ERROR, 409) from exc


def _recover(record, article, raw):
    _require(isinstance(record, dict) and isinstance(article, dict))
    _require(set(article) == {"url", "title", "original", "published_at"})
    _require(isinstance(record.get("id"), str)
             and re.fullmatch(r"[a-f0-9-]{36}", record["id"]))
    _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_TRACE_BYTES)
    _require(canonical_article_url(article["url"]) == article["url"] == record.get("url"))
    en_title, en_body, source = _validated_source(article["title"], article["original"])
    _require(en_title == article["title"] and en_body == article["original"])
    _require(_stamp(article["published_at"]) == record.get("source_published_at"))
    _require(_hash(record.get("input_hash")) == fingerprint(article))
    _require(_hash(record.get("original_hash")) == text_hash(article["original"]))
    attempts = record.get("preparation_attempts")
    _require(isinstance(attempts, list) and attempts
             and all(isinstance(item, dict) for item in attempts))
    attempt = attempts[-1]
    _require(attempt.get("operator_correction") is True
             and attempt.get("state") in {"needs_attention", "rejected", "failed"}
             and attempt.get("error") == "ARTICLE_QUALITY_REJECTED")
    _require(type(attempt.get("number")) is int and attempt["number"] == len(attempts))
    started, finished = _stamp(attempt.get("started_at")), _stamp(attempt.get("finished_at"))
    _require(_stamp(record.get("claimed_at")) <= started <= finished)
    expected_candidate = _hash(attempt.get("candidate_hash"))
    expected_raw = _hash(attempt.get("candidate_raw_hash"))

    lines = raw.decode("utf-8").splitlines()
    _require(lines and len(lines) <= 10000 and all(line.strip() for line in lines))
    events = [parse_json(line) for line in lines]
    previous = -1
    for event in events:
        _require(isinstance(event, dict) and event.get("record_id") == record["id"])
        stamp = _stamp(event.get("at"))
        _require(stamp >= previous and stamp <= finished)
        previous = stamp
    segment = [event for event in events if event["at"] >= started]
    phases = [event.get("phase") for event in segment]
    required = ["candidate_started", "gate_passed", "review_started", "review_returned",
                "review_parsed", "spelling_started", "spelling_returned",
                "spelling_adjudicated", "rejected"]
    if "review_normalized" in phases:
        required.insert(4, "review_normalized")
    _require(phases == required)
    _require(all(type(event.get("attempt")) is int and event["attempt"] == 1
                 for event in segment))
    by_phase = {event["phase"]: event for event in segment}
    first = by_phase["candidate_started"]
    _require(first.get("mode") == "candidate"
             and first.get("source_untrusted") == {
                 "title": en_title, "body": en_body, "url": article["url"]})
    candidate = first.get("candidate")
    _require(isinstance(candidate, dict) and set(candidate) == {"title", "body"})
    _require(all(isinstance(value, str) and value.strip() == value and value
                 for value in candidate.values()))
    raw_candidate = first.get("candidate_raw")
    if raw_candidate is None:
        # Historical traces checked their literal, already stripped candidate.
        _require(fingerprint(candidate) == expected_raw == expected_candidate)
    else:
        _require(isinstance(raw_candidate, dict) and set(raw_candidate) == {"title", "body"}
                 and all(isinstance(value, str) and value.strip() for value in raw_candidate.values()))
        _require(fingerprint(raw_candidate) == expected_raw
                 and {key: normalize_article_links(raw_candidate[key].strip()) for key in ("title", "body")} == candidate
                 and fingerprint(candidate) == expected_candidate)
    title, body = candidate["title"], candidate["body"]
    _require(len(title) <= 500 and "Источник:" not in body
             and "Перевод и редактура с помощью нейросетей." not in body)
    text = title + "\n\n" + body
    full = text + "\n\nИсточник: " + article["url"] + "\nПеревод и редактура с помощью нейросетей."
    translation_gate(source, text)
    _require(len(full) <= 3500 and by_phase["gate_passed"].get("characters") == len(full))
    context = by_phase["review_started"].get("context")
    _require(isinstance(context, dict) and context.get("source_untrusted") == source
             and context.get("translation_untrusted") == text)
    review_raw = by_phase["review_returned"].get("raw")
    _require(isinstance(review_raw, str) and review_raw.strip())
    parsed_raw = extract_object(review_raw)
    normalized = _normalize_review(parsed_raw)
    review = by_phase["review_parsed"].get("review")
    _require(review == normalized and review["approved"] is False
             and review["issues"] and review["topic"] in TOPICS)
    if parsed_raw != normalized:
        _require("review_normalized" in by_phase)
        event = by_phase["review_normalized"]
        _require(event.get("raw_review") == parsed_raw and event.get("review") == normalized)
    elif "review_normalized" in by_phase:
        raise AgentError(ERROR, 409)
    matches = by_phase["spelling_returned"].get("matches")
    _require(isinstance(matches, list) and all(isinstance(item, dict) for item in matches))
    adjudicated, unresolved = adjudicate_scientific_spelling(source, text, matches)
    spelling = by_phase["spelling_adjudicated"]
    _require(not unresolved and spelling.get("adjudicated") == adjudicated
             and spelling.get("unresolved") == unresolved)
    final = by_phase["rejected"]
    _require(final.get("mode") == "candidate"
             and final.get("error") == "ARTICLE_QUALITY_REJECTED"
             and final.get("feedback") == {
                 "review_approved": False, "topic": review["topic"],
                 "review_issues": review["issues"], "spelling_matches": []})
    evidence = {"review_raw": review_raw, "review": copy.deepcopy(review),
                "spelling_matches": copy.deepcopy(matches),
                "spelling_adjudicated": copy.deepcopy(adjudicated),
                "spelling_unresolved": [], "hard_gates_passed": True,
                "text_hash": fingerprint({"title": title, "body": full}),
                "source_hash": fingerprint({"title": en_title, "body": en_body,
                                             "url": article["url"]}),
                "trace_hash": hashlib.sha256(raw).hexdigest(),
                "segment_hash": fingerprint(segment)}
    return {"title": title, "body": full, "translation": text, "original": source,
            "url": article["url"], "topic": review["topic"],
            "machine_review_status": "rejected", "machine_review_evidence": evidence}
