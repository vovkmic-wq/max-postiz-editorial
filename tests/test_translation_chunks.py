"""Pure experimental framing tests; no network, inference or publication evidence."""
from dataclasses import replace
import hashlib
import json

import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.translation_chunks import (
    CHUNK_CONTRACT, PROBE_CONTRACT, SENTENCE_CONTRACT, ChunkProtocolError, make_probe_window,
    parse_sentence_chunk, parse_window, plan_chunks, plan_sentence_chunks, sentence_spans,
    stitch, stitch_sentence_chunks,
)


def article(count=12):
    return "\n\n".join(
        f"Observation {index} describes the red light filtered through Earth's atmosphere, "
        "while independent measurements preserve the original dates and physical relationships."
        for index in range(count))


def response(window, core="Точный перевод центрального фрагмента."):
    return "\n".join(block.opening + "\n" + (core if block.role == "core" else
        "Перевод соседнего контекста, который не принадлежит этому фрагменту.") + "\n" + block.closing
        for block in window.blocks)


def test_plan_owns_every_source_character_once_and_only_adds_adjacent_sentence_context():
    source = " \n" + article() + "  \n\n"
    plan = plan_chunks(source)
    assert len(plan.windows) > 2
    assert "".join(window.core.text for window in plan.windows) == source
    assert "".join(span.text for span in plan.sentences) == source
    cursor = 0
    for window in plan.windows:
        assert window.contract == CHUNK_CONTRACT and window.core.start == cursor
        assert source[window.core.start:window.core.end] == window.core.text
        assert len(window.core.text.strip()) <= 600 and len(window.input_text) <= 1600
        previous = next((block for block in window.blocks if block.role == "previous"), None)
        following = next((block for block in window.blocks if block.role == "following"), None)
        if previous:
            assert previous.span.end == window.core.start and previous.span in plan.sentences
        if following:
            assert following.span.start == window.core.end and following.span in plan.sentences
        cursor = window.core.end
    assert cursor == len(source)


def test_exact_identity_responses_reconstruct_all_source_layout_without_fuzzy_overlap_removal():
    source = "  " + article() + "\n\n  "
    plan = plan_chunks(source)
    parsed = [parse_window(window, window.input_text) for window in plan.windows]
    assert stitch(plan, parsed) == source


def test_stitch_discards_context_by_id_only_and_keeps_equal_owned_translations():
    plan = plan_chunks(article())
    parsed = [parse_window(window, response(window, core="Повторяющееся верное предложение."))
              for window in plan.windows]
    result = stitch(plan, parsed)
    assert result.count("Повторяющееся верное предложение.") == len(plan.windows)
    assert "соседнего контекста" not in result


def test_sentence_boundaries_do_not_split_abbreviations_decimals_or_literal_urls():
    source = ("Dr. A. Smith, Ph.D. at the U.S. observatory used Fig. 2 with a 3.14 ratio and "
              "[data](https://example.org/path_(item)/ID-01?x=1.25#part). "
              "The second sentence preserves the 2,300 km distance.")
    spans = sentence_spans(source)
    assert len(spans) == 2
    assert spans[0].text.endswith("#part). ")
    assert "https://example.org/path_(item)/ID-01?x=1.25#part" in spans[0].text
    assert spans[1].text == "The second sentence preserves the 2,300 km distance."


def test_short_semantic_units_are_explicit_instead_of_splitting_a_sentence():
    source = "A" * 349 + ". " + "B" * 309 + ". " + "C" * 449 + "."
    plan = plan_chunks(source)
    assert plan.windows[0].short_core_reason == "sentence_boundary"
    assert plan.windows[-1].short_core_reason is None
    assert [len(window.core.text.strip()) for window in plan.windows] == [350, 310, 450]


def test_short_title_and_final_tail_have_documented_reasons():
    title = plan_chunks("Lunar eclipses flower.")
    assert title.windows[0].short_core_reason == "short_source"
    tail = plan_chunks("A" * 449 + ". " + "A short final sentence.")
    # Both sentences fit in one core; a real tail requires a preceding full core.
    assert len(tail.windows) == 1
    tail = plan_chunks("A" * 589 + ". " + "A short final sentence.")
    assert tail.windows[-1].short_core_reason == "tail"


@pytest.mark.parametrize("source", [None, "", " \n ", "x" * 16001, "\ud800",
                                     "Source [[m000001]] collision.", "Source [[other]] collision."],
                         ids=["none", "empty", "whitespace", "overlong", "surrogate", "reserved_id", "reserved_brackets"])
def test_invalid_source_or_reserved_namespace_is_not_sanitized_or_translated(source):
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_INPUT_INVALID"):
        plan_chunks(source)


@pytest.mark.parametrize("config", [{"max_core": 601}, {"min_core": 399}, {"window_limit": 1601},
                                    {"min_core": True}, {"max_core": 500, "min_core": 550}])
def test_invalid_config_does_not_change_the_contract(config):
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_CONFIG_INVALID"):
        plan_chunks(article(), **config)


def test_overlong_sentence_fails_without_word_cut_or_fallback():
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_SENTENCE_TOO_LONG"):
        plan_chunks("A complete but overlong scientific sentence " + "word " * 150 + ".")


def test_oversized_full_context_window_is_not_truncated_or_silently_dropped():
    source = " ".join(chr(65 + index) * 549 + "." for index in range(3))
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_CONTEXT_TOO_LARGE"):
        plan_chunks(source)


def test_window_limit_counts_exact_framing_not_only_english_text():
    window = make_probe_window("Core observation.", previous="Previous observation.", following="Next observation.")
    assert window.contract == PROBE_CONTRACT
    assert window.evidence()["input_sha256"] == hashlib.sha256(window.input_text.encode()).hexdigest()
    assert len(window.input_text) > sum(len(block.span.text) for block in window.blocks)
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_CONTEXT_TOO_LARGE"):
        make_probe_window("A" * 1500, previous="B" * 100, following="C" * 100)


@pytest.mark.parametrize("mutation", ["missing", "translated", "duplicate", "reordered", "nested",
                                     "unknown_id", "malformed", "outside_prefix", "outside_suffix", "empty"])
def test_response_markers_must_be_exact_unique_ordered_and_complete(mutation):
    window = make_probe_window("The core contains 14%.", previous="The preceding fact.", following="The next fact.")
    raw = response(window)
    first, core, last = window.blocks
    if mutation == "missing":
        raw = raw.replace(core.closing, "")
    elif mutation == "translated":
        raw = raw.replace(core.opening, "[[м000001]]")
    elif mutation == "duplicate":
        raw = raw.replace(core.closing, core.closing + core.closing)
    elif mutation == "reordered":
        raw = raw.replace(core.opening, "TEMP").replace(first.opening, core.opening).replace("TEMP", first.opening)
    elif mutation == "nested":
        raw = raw.replace(first.closing, "").replace(core.closing, core.closing + first.closing)
    elif mutation == "unknown_id":
        raw = raw.replace(core.opening, "[[m999999]]")
    elif mutation == "malformed":
        raw = raw.replace("Точный", "[[m12x]] Точный")
    elif mutation == "outside_prefix":
        raw = "Unowned additional claim.\n" + raw
    elif mutation == "outside_suffix":
        raw += "\nUnowned additional claim."
    else:
        raw = raw.replace("Точный перевод центрального фрагмента.", "  ")
    with pytest.raises(ChunkProtocolError) as failure:
        parse_window(window, raw)
    evidence = failure.value.evidence()
    assert failure.value.status == 409 and evidence["outcome"] == "known_protocol_failure"
    assert evidence["raw_output"] == raw
    assert evidence["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert evidence["window"] == window.evidence()
    assert last.ident != core.ident


@pytest.mark.parametrize("raw", [None, "", "  ", "x" * 32769], ids=["none", "empty", "whitespace", "overlong"])
def test_invalid_response_preserves_failure_payload(raw):
    window = make_probe_window("A source fact.")
    with pytest.raises(ChunkProtocolError) as failure:
        parse_window(window, raw)
    assert failure.value.evidence()["raw_output"] == raw


@pytest.mark.parametrize("damage", ["missing", "duplicate", "reorder", "core", "plan_input", "source"])
def test_stitch_requires_the_exact_plan_and_every_raw_bound_owned_window_once(damage):
    plan = plan_chunks(article())
    parsed = [parse_window(window, response(window)) for window in plan.windows]
    if damage == "missing":
        parsed.pop()
    elif damage == "duplicate":
        parsed[1] = parsed[0]
    elif damage == "reorder":
        parsed.reverse()
    elif damage == "core":
        parsed[0] = replace(parsed[0], core_translation="An invented unbound claim.")
    elif damage == "plan_input":
        window = replace(plan.windows[0], input_text=plan.windows[0].input_text + "extra")
        plan = replace(plan, windows=(window, *plan.windows[1:]))
    else:
        plan = replace(plan, source=plan.source + "changed")
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_STITCH_INVALID"):
        stitch(plan, parsed)


def test_full_evidence_retains_raw_contexts_windows_offsets_hashes_and_output():
    plan = plan_chunks(article())
    parsed = [parse_window(window, response(window)) for window in plan.windows]
    proof = plan.evidence()
    assert proof["offset_unit"] == "unicode_codepoint"
    assert proof["source"] == plan.source
    assert proof["source_sha256"] == hashlib.sha256(plan.source.encode()).hexdigest()
    assert proof["plan_hash"] == fingerprint({k: v for k, v in proof.items() if k != "plan_hash"})
    for window, result in zip(plan.windows, parsed, strict=True):
        saved = result.evidence()
        assert saved["window_hash"] == fingerprint(window.evidence())
        assert saved["raw_output"] == result.raw_output
        assert len(saved["translated_blocks"]) == len(window.blocks)


def test_explicit_context_off_plan_covers_source_once_without_markers_or_overlap():
    source = " \n" + article() + "  \n"
    plan = plan_sentence_chunks(source)
    assert plan.contract == SENTENCE_CONTRACT
    assert "".join(chunk.core.text for chunk in plan.windows) == source
    assert all(len(chunk.blocks) == 1 and chunk.blocks[0].role == "core" for chunk in plan.windows)
    assert all("[[" not in chunk.input_text and len(chunk.input_text) <= 600 for chunk in plan.windows)
    parsed = [parse_sentence_chunk(chunk, chunk.input_text) for chunk in plan.windows]
    assert stitch_sentence_chunks(plan, parsed) == source
    assert all(result.evidence()["raw_output"] == chunk.input_text
               for chunk, result in zip(plan.windows, parsed, strict=True))


def test_context_off_is_separately_selected_and_not_an_implicit_response_fallback():
    source = " ".join(chr(65 + index) * 549 + "." for index in range(3))
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_CONTEXT_TOO_LARGE"):
        plan_chunks(source)
    plain = plan_sentence_chunks(source)
    assert len(plain.windows) == 3
    framed = make_probe_window("A factual core.", previous="A previous fact.")
    with pytest.raises(ChunkProtocolError):
        parse_window(framed, "Фактический центральный фрагмент без маркеров.")
    with pytest.raises(ChunkProtocolError, match="TRANSLATION_CHUNK_PROTOCOL_INVALID"):
        parse_sentence_chunk(framed, "A response cannot switch the requested contract.")
    with pytest.raises(ChunkProtocolError, match="TRANSLATION_CHUNK_PROTOCOL_INVALID"):
        parse_window(plain.windows[0], plain.windows[0].input_text)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "reorder", "raw_change", "wrong_plan"])
def test_context_off_stitch_requires_every_exact_raw_bound_core_once(damage):
    plan = plan_sentence_chunks(article())
    parsed = [parse_sentence_chunk(chunk, "Перевод принадлежащего этому запросу фрагмента.")
              for chunk in plan.windows]
    if damage == "missing":
        parsed.pop()
    elif damage == "duplicate":
        parsed[1] = parsed[0]
    elif damage == "reorder":
        parsed.reverse()
    elif damage == "raw_change":
        parsed[0] = replace(parsed[0], raw_output="Чужой ответ при прежнем hash.")
    else:
        plan = plan_chunks(article())
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_STITCH_INVALID"):
        stitch_sentence_chunks(plan, parsed)


def test_context_off_keeps_equal_owned_sentences_and_still_rejects_overlong_source():
    plan = plan_sentence_chunks(article())
    parsed = [parse_sentence_chunk(chunk, "Одинаковый перевод.") for chunk in plan.windows]
    assert stitch_sentence_chunks(plan, parsed).count("Одинаковый перевод.") == len(plan.windows)
    with pytest.raises(AgentError, match="TRANSLATION_CHUNK_SENTENCE_TOO_LONG"):
        plan_sentence_chunks("long " * 150 + ".")


@pytest.mark.parametrize("framed", [True, False])
def test_invalid_unicode_response_is_typed_and_losslessly_serializable(framed):
    if framed:
        window = make_probe_window("A source fact.")
        raw = response(window, core="\ud800")
        parser = parse_window
    else:
        window = plan_sentence_chunks("A source fact.").windows[0]
        raw, parser = "\ud800", parse_sentence_chunk
    with pytest.raises(ChunkProtocolError) as failed:
        parser(window, raw)
    assert failed.value.reason == "invalid_unicode_response"
    evidence = failed.value.evidence()
    assert evidence["raw_output"] is None and evidence["raw_sha256"] is None
    assert json.loads(evidence["raw_output_json"]) == raw
    json.dumps(evidence, ensure_ascii=False).encode("utf-8")
