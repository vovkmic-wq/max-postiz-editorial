"""Первичный отказ Hy нельзя пересэмплировать через операторский candidate."""
import copy
import json

import pytest

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Draft
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant

pytest_plugins = ["test_automatic_policy"]


@pytest.fixture
def initial_refusal(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    cycle, core, article, records, writes, _, _ = values
    counts = {"hy": 0, "min": 0, "lt": 0}
    original_quality = cycle._quality

    def quality(*args, **kwargs):
        result = original_quality(*args, **kwargs)
        for model, key in ((result.translator, "hy"), (result.reviewer, "min")):
            original = model.generate_text

            def counted(prompt, original=original, key=key):
                counts[key] += 1
                return original(prompt)

            model.generate_text = counted
        spelling = result._spelling_matches

        def checked(text):
            counts["lt"] += 1
            return spelling(text)

        result._spelling_matches = checked
        return result

    monkeypatch.setattr(cycle, "_quality", quality)

    def reject(*, markdown=False):
        if markdown:
            article["original"] += " Read [the study](https://science.nasa.gov/research/)."
            prior = cycle._quality

            def linked(*args, **kwargs):
                result = prior(*args, **kwargs)
                result.translator.value["body"] += " [Исследование](https://science.nasa.gov/research/)."
                return result

            monkeypatch.setattr(cycle, "_quality", linked)
        review.update(approved=False, issues=["The translation omits a scientific fact."])
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            cycle.prepare(article)
        record = copy.deepcopy(cycle._load()["records"][0])
        path = core.settings.root / record["quality_trace"]
        raw = path.read_bytes()
        events = [json.loads(line) for line in raw.decode().splitlines()]
        candidate = next(event["translation"] for event in events if event["phase"] == "translation_parsed")
        assert counts == {"hy": 1, "min": 1, "lt": 1}
        assert not records and not writes and not record.get("variant_id")
        return record, path, raw, events, candidate

    return values, review, counts, reject


def no_business_state(core):
    with core.db.session() as session:
        assert session.query(Draft).count() == session.query(Variant).count() == 0
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert session.query(BridgePost).count() == 0
        assert session.get(Target, "max").paused


@pytest.mark.parametrize("form", ["exact", "padding", "internal_whitespace", "markdown_normalized"])
def test_initial_hy_refusal_blocks_equivalent_candidate_before_any_second_review(initial_refusal, form):
    (cycle, core, article, records, writes, _, _), review, counts, reject = initial_refusal
    record, path, raw, _, candidate = reject(markdown=form == "markdown_normalized")
    if form == "padding":
        candidate = {key: " \n" + value + "\n " for key, value in candidate.items()}
    elif form == "internal_whitespace":
        candidate = {key: value.replace(" ", "  \n\t") for key, value in candidate.items()}
    # Даже будущий положительный ответ не должен быть запрошен.
    review.update(approved=True, issues=[])
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        cycle.prepare(article, retry_record=record["id"], candidate=candidate)
    current = cycle._load()["records"][0]
    assert current["preparation_attempts"] == record["preparation_attempts"]
    assert current["state"] == "needs_attention" and current["error"] == "ARTICLE_QUALITY_REJECTED"
    assert current["preparation_actions"][-1]["action"] == "rejected_candidate_replay_blocked"
    assert path.read_bytes() == raw and counts == {"hy": 1, "min": 1, "lt": 1}
    assert not records and not writes
    no_business_state(core)


def test_changed_candidate_gets_fresh_review_and_lt_without_retranslation_or_lost_refusal(initial_refusal):
    (cycle, core, article, records, writes, _, _), review, counts, reject = initial_refusal
    record, path, raw, _, candidate = reject()
    candidate["body"] = "Учёные изучили необычные процессы в атмосфере далёкой звезды и свойства звёздного ветра."
    review.update(approved=True, issues=[])
    result = cycle.prepare(article, retry_record=record["id"], candidate=candidate)
    assert result["state"] == "automatic_ready" and result["id"] == record["id"]
    assert result["preparation_attempts"][0] == record["preparation_attempts"][0]
    assert result["preparation_attempts"][1]["operator_correction"] is True
    assert path.read_bytes().startswith(raw) and counts == {"hy": 1, "min": 2, "lt": 2}
    assert len(records) == len(writes) == 1 and records[result["postiz_id"]]["state"] == "DRAFT"
    with core.db.session() as session:
        assert session.query(Draft).count() == session.query(Variant).count() == 1
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert session.get(Target, "max").paused


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("changed_again", [False, True])
def test_later_operator_refusal_blocks_reflow_but_accepts_real_new_candidate(initial_refusal, legacy, changed_again):
    (cycle, core, article, records, writes, _, _), review, counts, reject = initial_refusal
    first, path, raw_first, _, candidate = reject()
    candidate["body"] += " Дополнены свойства звёздного ветра."
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article, retry_record=first["id"], candidate=candidate)
    assert counts == {"hy": 1, "min": 2, "lt": 2}
    ledger = cycle._load()
    if legacy:
        # Исторический полный trace есть, replay hash ещё не сохранялся.
        ledger["records"][0]["preparation_attempts"][-1].pop("candidate_replay_hash")
        cycle._save(ledger)
    attempts = copy.deepcopy(cycle._load()["records"][0]["preparation_attempts"])
    raw = path.read_bytes()
    assert raw.startswith(raw_first) and attempts[-1]["error"] == "ARTICLE_QUALITY_REJECTED"
    supplied = {key: value.replace(" ", " \n\t ") for key, value in candidate.items()}
    review.update(approved=True, issues=[])
    if changed_again:
        supplied = dict(candidate)
        supplied["body"] += " Исправлен пропущенный научный факт."
        ready = cycle.prepare(article, retry_record=first["id"], candidate=supplied)
        assert ready["state"] == "automatic_ready"
        assert ready["preparation_attempts"][:2] == attempts
        assert counts == {"hy": 1, "min": 3, "lt": 3} and path.read_bytes().startswith(raw)
        assert len(records) == len(writes) == 1 and records[ready["postiz_id"]]["state"] == "DRAFT"
    else:
        def forbidden_factory():
            pytest.fail("Отказ B после перезапуска не разрешает новую оценку reflow B")

        restarted = HourlyArticleCycle(core, forbidden_factory)
        with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
            restarted.prepare(article, retry_record=first["id"], candidate=supplied)
        assert counts == {"hy": 1, "min": 2, "lt": 2} and path.read_bytes() == raw
        assert cycle._load()["records"][0]["preparation_attempts"] == attempts
        assert not records and not writes
        no_business_state(core)


@pytest.mark.parametrize("damage", ["missing_candidate_event", "wrong_source", "malformed_replay_hash"])
def test_legacy_operator_refusal_requires_its_own_proof_even_with_valid_initial_trace(initial_refusal, damage):
    (cycle, core, article, records, writes, _, _), review, counts, reject = initial_refusal
    first, path, raw_first, _, candidate = reject()
    candidate["body"] += " Дополнены свойства звёздного ветра."
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article, retry_record=first["id"], candidate=candidate)
    ledger = cycle._load()
    attempt = ledger["records"][0]["preparation_attempts"][-1]
    if damage == "malformed_replay_hash":
        attempt["candidate_replay_hash"] = "invalid"
    else:
        attempt.pop("candidate_replay_hash")
        if damage == "missing_candidate_event":
            path.write_bytes(raw_first)
        else:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            next(event for event in events if event["phase"] == "candidate_started")["source_untrusted"]["body"] += " A different source."
            path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")
    cycle._save(ledger)
    before = path.read_bytes()
    review.update(approved=True, issues=[])
    error = "ARTICLE_LEDGER_INVALID" if damage == "malformed_replay_hash" else "ARTICLE_REJECTED_CANDIDATE_TRACE_REQUIRED"
    with pytest.raises(AgentError, match=error):
        cycle.prepare(article, retry_record=first["id"], candidate={key: value.replace(" ", "  \n") for key, value in candidate.items()})
    assert counts == {"hy": 1, "min": 2, "lt": 2} and not records and not writes
    assert path.read_bytes() == before and cycle._load()["records"][0]["preparation_attempts"] == ledger["records"][0]["preparation_attempts"]
    no_business_state(core)


@pytest.mark.parametrize("damage", [
    "missing", "different_record", "source_body", "source_title", "source_url", "prompt", "numeric_context",
    "raw_translation", "null_translation", "parsed_translation", "no_terminal", "terminal_error", "terminal_attempt",
    "terminal_feedback", "terminal_mode", "outside_window", "reversed_window", "missing_finished",
    "review_source", "review_translation", "duplicate_json_key", "different_path", "ambiguous_segments",
])
def test_initial_refusal_without_bound_complete_trace_fails_closed_before_models(initial_refusal, damage):
    (cycle, core, article, records, writes, _, _), review, counts, reject = initial_refusal
    record, path, raw, events, candidate = reject()
    initial, parsed, returned = events[0], next(e for e in events if e["phase"] == "translation_parsed"), events[1]
    if damage == "missing":
        path.unlink()
    elif damage == "different_record":
        initial["record_id"] = "00000000-0000-0000-0000-000000000000"
    elif damage.startswith("source_"):
        initial["context"]["source_untrusted"][damage.removeprefix("source_")] += " changed"
    elif damage == "prompt":
        initial["context"]["instruction"] += " changed"
    elif damage == "numeric_context":
        initial["context"]["numeric_tokens_must_match_exactly"] = ["999"]
    elif damage == "raw_translation":
        returned["raw"] = json.dumps({"title": "Чужой текст", "body": candidate["body"]})
    elif damage == "null_translation":
        returned["raw"] = None
    elif damage == "parsed_translation":
        parsed["translation"]["body"] += " Чужой текст."
    elif damage == "no_terminal":
        events.pop()
    elif damage == "terminal_error":
        events[-1]["error"] = "ARTICLE_SPELLING_UNAVAILABLE"
    elif damage == "terminal_attempt":
        events[-1]["attempt"] = True
    elif damage == "terminal_feedback":
        events[-1]["feedback"] = "different"
    elif damage == "terminal_mode":
        events[-1]["mode"] = "candidate"
    elif damage in {"outside_window", "reversed_window", "missing_finished", "different_path"}:
        ledger = cycle._load()
        if damage == "different_path":
            ledger["records"][0]["quality_trace"] = "data/borrowed-trace.local.jsonl"
        else:
            attempt = ledger["records"][0]["preparation_attempts"][0]
            if damage == "missing_finished":
                attempt.pop("finished_at")
            elif damage == "reversed_window":
                attempt["finished_at"] = attempt["started_at"] - 1
            else:
                attempt["started_at"] -= 2
                attempt["finished_at"] -= 1
        cycle._save(ledger)
    elif damage in {"review_source", "review_translation"}:
        context = next(e["context"] for e in events if e["phase"] == "review_started")
        context["source_untrusted" if damage == "review_source" else "translation_untrusted"] += " changed"
    elif damage == "ambiguous_segments":
        events += copy.deepcopy(events)
    if damage not in {"missing", "different_path", "outside_window", "reversed_window", "missing_finished"}:
        data = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"
        if damage == "duplicate_json_key":
            data = data.replace('"phase": "translation_started"', '"phase": "translation_started", "phase": "translation_started"', 1)
        path.write_text(data, encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    history = copy.deepcopy(cycle._load()["records"][0]["preparation_attempts"])
    review.update(approved=True, issues=[])
    changed = {**candidate, "body": candidate["body"] + " Дополнены свойства звёздного ветра."}
    with pytest.raises(AgentError, match="ARTICLE_REJECTED_CANDIDATE_TRACE_REQUIRED"):
        cycle.prepare(article, retry_record=record["id"], candidate=changed)
    assert counts == {"hy": 1, "min": 1, "lt": 1} and not records and not writes
    assert cycle._load()["records"][0]["preparation_attempts"] == history
    assert (path.read_bytes() if path.exists() else None) == before
    no_business_state(core)
