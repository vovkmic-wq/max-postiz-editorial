"""Experimental owned-core windows; marker validity never establishes translation quality.

The caller must durably save plan/input and every complete raw response BEFORE
parsing. Missing or changed framing is a known protocol failure, never permission
to retry, guess alignment, drop a sentence or enable an untested fallback.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from .config import AgentError, fingerprint

CHUNK_CONTRACT = "madlad-owned-context-v1-experimental"
SENTENCE_CONTRACT = "madlad-owned-sentences-v1-experimental"
PROBE_CONTRACT = "madlad-owned-context-framing-probe-v1"
FRAMING = "ascii-m-blocks-v1"
NO_FRAMING = "none-owned-core-v1"
_MARKER = re.compile(r"\[\[(?P<close>/)?m[0-9]{6}\]\]")
_RESERVED = re.compile(r"\[\[|\]\]|\[/?[mM][0-9]")
_URL_START = re.compile(r"https?://", re.IGNORECASE)
_ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "no", "fig", "figs", "eq", "eqs", "vol", "al", "ph.d",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"})


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    text: str

    def evidence(self):
        return {"start": self.start, "end": self.end, "text": self.text, "sha256": _sha(self.text)}


@dataclass(frozen=True)
class WindowBlock:
    ident: str
    role: str
    span: SourceSpan

    @property
    def opening(self):
        return "[[" + self.ident + "]]"

    @property
    def closing(self):
        return "[[/" + self.ident + "]]"

    def evidence(self):
        return {"id": self.ident, "role": self.role, "span": self.span.evidence(),
                "opening": self.opening, "closing": self.closing}


@dataclass(frozen=True)
class TranslationWindow:
    contract: str
    index: int
    source_sha256: str
    core: SourceSpan
    blocks: tuple[WindowBlock, ...]
    input_text: str
    prefix: str
    suffix: str
    short_core_reason: str | None = None
    framing: str = FRAMING

    def evidence(self):
        return {"contract": self.contract, "framing": self.framing, "index": self.index,
            "offset_unit": "unicode_codepoint",
            "source_sha256": self.source_sha256, "core": self.core.evidence(),
            "blocks": [block.evidence() for block in self.blocks], "input_text": self.input_text,
            "input_sha256": _sha(self.input_text), "prefix": self.prefix, "suffix": self.suffix,
            "short_core_reason": self.short_core_reason}


@dataclass(frozen=True)
class TranslationPlan:
    source: str
    sentences: tuple[SourceSpan, ...]
    windows: tuple[TranslationWindow, ...]
    min_core: int
    max_core: int
    window_limit: int
    contract: str = CHUNK_CONTRACT
    framing: str = FRAMING

    def evidence(self):
        value = {"contract": self.contract, "framing": self.framing, "source": self.source,
            "offset_unit": "unicode_codepoint",
            "source_sha256": _sha(self.source), "min_core": self.min_core, "max_core": self.max_core,
            "window_limit": self.window_limit, "sentences": [span.evidence() for span in self.sentences],
            "windows": [window.evidence() for window in self.windows]}
        return {**value, "plan_hash": fingerprint(value)}


@dataclass(frozen=True)
class ParsedWindow:
    index: int
    window_hash: str
    raw_output: str
    core_translation: str
    blocks: tuple[tuple[str, str], ...]

    def evidence(self):
        return {"index": self.index, "window_hash": self.window_hash, "raw_output": self.raw_output,
            "raw_sha256": _sha(self.raw_output), "core_translation": self.core_translation,
            "core_translation_sha256": _sha(self.core_translation),
            "translated_blocks": [{"id": ident, "text": text, "sha256": _sha(text)}
                                  for ident, text in self.blocks]}


class ChunkProtocolError(AgentError):
    def __init__(self, window, raw_output, reason):
        super().__init__("TRANSLATION_CHUNK_PROTOCOL_INVALID", 409)
        self.window, self.raw_output, self.reason = window, raw_output, reason

    def evidence(self):
        raw, raw_hash, escaped = self.raw_output, None, None
        if isinstance(raw, str):
            try:
                raw_hash = _sha(raw)
            except UnicodeError:
                # JSON escapes retain an invalid decoded string losslessly while
                # keeping the failure evidence itself UTF-8 serializable.
                escaped, raw = json.dumps(raw, ensure_ascii=True), None
        return {"window": self.window.evidence(), "raw_output": raw,
            "raw_output_json": escaped, "raw_sha256": raw_hash,
            "error": self.code, "reason": self.reason, "outcome": "known_protocol_failure"}


def _protected(text):
    spans = []
    cursor = 0
    for match in _URL_START.finditer(text):
        if match.start() < cursor:
            continue
        start = end = match.start()
        wrapped = start > 0 and text[start - 1] == "("
        depth = 0
        while end < len(text):
            char = text[end]
            if char.isspace() or char in '<>"':
                break
            if char == "(":
                depth += 1
            elif char == ")":
                if depth:
                    depth -= 1
                elif wrapped:
                    break
            end += 1
        spans.append((start, end))
        cursor = end
    spans.extend(match.span() for match in re.finditer(r"\b(?:[A-Za-z]\.){2,}", text))
    spans.extend(match.span() for match in re.finditer(r"(?<!\w)[A-Za-z]\.(?=\s*[A-Z])", text))
    for match in re.finditer(r"(?<!\w)([A-Za-z]+(?:\.[A-Za-z]+)*)\.", text):
        if match[1].casefold() in _ABBREVIATIONS:
            spans.append(match.span())
    spans.extend(match.span() for match in re.finditer(r"(?<=\d)\.(?=\d)", text))
    return spans


def sentence_spans(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 16000 or _RESERVED.search(text):
        raise AgentError("TRANSLATION_CHUNK_INPUT_INVALID", 409)
    try:
        text.encode("utf-8")
    except UnicodeError as exc:
        raise AgentError("TRANSLATION_CHUNK_INPUT_INVALID", 409) from exc
    protected = _protected(text)
    boundaries = {match.end() for match in re.finditer(r"\n[ \t\r]*\n(?:[ \t\r]*\n)*", text)}
    for match in re.finditer(r"[.!?…]+[\"'’”\)\]]*(?=\s|$)", text):
        if any(start <= match.start() < end for start, end in protected):
            continue
        end = match.end()
        while end < len(text) and text[end].isspace():
            end += 1
        boundaries.add(end)
    spans, start = [], 0
    for end in sorted(boundaries | {len(text)}):
        if end > start and text[start:end].strip():
            spans.append(SourceSpan(start, end, text[start:end]))
            start = end
    if start != len(text):
        last = spans[-1]
        spans[-1] = SourceSpan(last.start, len(text), text[last.start:])
    return tuple(spans)


def _window(source, index, core, previous, following, *, contract, limit, reason=None):
    spans = (("previous", previous), ("core", core), ("following", following))
    blocks = tuple(WindowBlock(f"m{index:04d}{position:02d}", role, span)
                   for position, (role, span) in enumerate(spans) if span is not None)
    framed = "\n".join(block.opening + "\n" + block.span.text.strip() + "\n" + block.closing
                       for block in blocks)
    if len(framed) > limit:
        raise AgentError("TRANSLATION_CHUNK_CONTEXT_TOO_LARGE", 409)
    prefix = core.text[:len(core.text) - len(core.text.lstrip())]
    suffix = core.text[len(core.text.rstrip()):]
    return TranslationWindow(contract, index, _sha(source), core, blocks, framed, prefix, suffix, reason)


def make_probe_window(core, *, previous="", following=""):
    """Candidate framing for ONE explicit model probe; never production enablement."""
    if (any(not isinstance(part, str) for part in (previous, core, following)) or not core.strip()
            or _RESERVED.search(previous + core + following)):
        raise AgentError("TRANSLATION_CHUNK_INPUT_INVALID", 409)
    source = previous + core + following
    try:
        source.encode("utf-8")
    except UnicodeError as exc:
        raise AgentError("TRANSLATION_CHUNK_INPUT_INVALID", 409) from exc
    before = SourceSpan(0, len(previous), previous) if previous.strip() else None
    owned = SourceSpan(len(previous), len(previous) + len(core), core)
    after = SourceSpan(owned.end, len(source), following) if following.strip() else None
    return _window(source, 0, owned, before, after, contract=PROBE_CONTRACT, limit=1600)


def _config(min_core, max_core, window_limit):
    if (type(min_core) is not int or type(max_core) is not int or type(window_limit) is not int
            or not 400 <= min_core <= max_core <= 600 or not 1 <= window_limit <= 1600):
        raise AgentError("TRANSLATION_CHUNK_CONFIG_INVALID", 409)


def _core_ranges(sentences, max_core):
    if any(len(span.text.strip()) > max_core for span in sentences):
        raise AgentError("TRANSLATION_CHUNK_SENTENCE_TOO_LONG", 409)
    ranges, first = [], 0
    while first < len(sentences):
        last = first
        while last + 1 < len(sentences) and sentences[last + 1].end - sentences[first].start <= max_core:
            last += 1
        ranges.append((first, last))
        first = last + 1
    return ranges


def _short_reason(core, index, ranges, min_core):
    if len(core.text) >= min_core:
        return None
    return "short_source" if len(ranges) == 1 else "tail" if index == len(ranges) - 1 else "sentence_boundary"


def plan_chunks(text, *, min_core=400, max_core=600, window_limit=1600):
    _config(min_core, max_core, window_limit)
    sentences = sentence_spans(text)
    ranges = _core_ranges(sentences, max_core)
    windows = []
    for index, (first, last) in enumerate(ranges):
        core = SourceSpan(sentences[first].start, sentences[last].end,
                          text[sentences[first].start:sentences[last].end])
        reason = _short_reason(core, index, ranges, min_core)
        windows.append(_window(text, index, core, sentences[first - 1] if first else None,
            sentences[last + 1] if last + 1 < len(sentences) else None,
            contract=CHUNK_CONTRACT, limit=window_limit, reason=reason))
    return TranslationPlan(text, sentences, tuple(windows), min_core, max_core, window_limit)


def plan_sentence_chunks(text, *, min_core=400, max_core=600, window_limit=1600):
    """Explicit context-off experiment, NEVER a fallback after a framed request."""
    _config(min_core, max_core, window_limit)
    sentences = sentence_spans(text)
    ranges = _core_ranges(sentences, max_core)
    windows = []
    for index, (first, last) in enumerate(ranges):
        core = SourceSpan(sentences[first].start, sentences[last].end,
                          text[sentences[first].start:sentences[last].end])
        prefix = core.text[:len(core.text) - len(core.text.lstrip())]
        suffix = core.text[len(core.text.rstrip()):]
        if len(core.text.strip()) > window_limit:
            raise AgentError("TRANSLATION_CHUNK_CONTEXT_TOO_LARGE", 409)
        windows.append(TranslationWindow(SENTENCE_CONTRACT, index, _sha(text), core,
            (WindowBlock(f"m{index:04d}01", "core", core),), core.text.strip(), prefix, suffix,
            _short_reason(core, index, ranges, min_core), NO_FRAMING))
    return TranslationPlan(text, sentences, tuple(windows), min_core, max_core, window_limit,
                           SENTENCE_CONTRACT, NO_FRAMING)


def parse_window(window, raw_output):
    """Only exact ordered unique markers authorize extraction of an owned core."""
    if window.contract not in {CHUNK_CONTRACT, PROBE_CONTRACT} or window.framing != FRAMING:
        raise ChunkProtocolError(window, raw_output, "wrong_contract")
    if not isinstance(raw_output, str) or not raw_output.strip() or len(raw_output) > 32768:
        raise ChunkProtocolError(window, raw_output, "invalid_response")
    try:
        raw_output.encode("utf-8")
    except UnicodeError as exc:
        raise ChunkProtocolError(window, raw_output, "invalid_unicode_response") from exc
    matches = list(_MARKER.finditer(raw_output))
    expected = [token for block in window.blocks for token in (block.opening, block.closing)]
    observed = [match[0] for match in matches]
    if observed != expected:
        raise ChunkProtocolError(window, raw_output, "missing_moved_duplicate_or_foreign_marker")
    if _RESERVED.search(_MARKER.sub("", raw_output)):
        raise ChunkProtocolError(window, raw_output, "malformed_marker")
    parsed, cursor = [], 0
    for index, block in enumerate(window.blocks):
        opening, closing = matches[index * 2:index * 2 + 2]
        if raw_output[cursor:opening.start()].strip():
            raise ChunkProtocolError(window, raw_output, "unowned_text_outside_blocks")
        value = raw_output[opening.end():closing.start()].strip()
        if not value:
            raise ChunkProtocolError(window, raw_output, "empty_block")
        parsed.append((block.ident, value))
        cursor = closing.end()
    if raw_output[cursor:].strip():
        raise ChunkProtocolError(window, raw_output, "unowned_text_after_blocks")
    core_id = next(block.ident for block in window.blocks if block.role == "core")
    return ParsedWindow(window.index, fingerprint(window.evidence()), raw_output,
                        dict(parsed)[core_id], tuple(parsed))


def parse_sentence_chunk(window, raw_output):
    if window.contract != SENTENCE_CONTRACT or window.framing != NO_FRAMING:
        raise ChunkProtocolError(window, raw_output, "wrong_contract")
    if (not isinstance(raw_output, str) or not raw_output.strip() or len(raw_output) > 32768
            or _RESERVED.search(raw_output)):
        raise ChunkProtocolError(window, raw_output, "invalid_unframed_response")
    try:
        raw_output.encode("utf-8")
    except UnicodeError as exc:
        raise ChunkProtocolError(window, raw_output, "invalid_unicode_response") from exc
    value = raw_output.strip()
    return ParsedWindow(window.index, fingerprint(window.evidence()), raw_output, value,
                        ((window.blocks[0].ident, value),))


def stitch(plan, parsed):
    """Join owned cores once; never compare or discard translated text fuzzily."""
    return _stitch(plan, parsed, CHUNK_CONTRACT, plan_chunks, parse_window)


def stitch_sentence_chunks(plan, parsed):
    return _stitch(plan, parsed, SENTENCE_CONTRACT, plan_sentence_chunks, parse_sentence_chunk)


def _stitch(plan, parsed, contract, planner, parser):
    if not isinstance(plan, TranslationPlan) or plan.contract != contract:
        raise AgentError("TRANSLATION_CHUNK_STITCH_INVALID", 409)
    expected = planner(plan.source, min_core=plan.min_core, max_core=plan.max_core,
                       window_limit=plan.window_limit)
    if fingerprint(expected.evidence()) != fingerprint(plan.evidence()) or len(parsed) != len(plan.windows):
        raise AgentError("TRANSLATION_CHUNK_STITCH_INVALID", 409)
    if "".join(window.core.text for window in plan.windows) != plan.source:
        raise AgentError("TRANSLATION_CHUNK_STITCH_INVALID", 409)
    output, cursor = [], 0
    for window, result in zip(plan.windows, parsed, strict=True):
        if (window.contract != contract or window.core.start != cursor
                or window.core.end != window.core.start + len(window.core.text)
                or plan.source[window.core.start:window.core.end] != window.core.text
                or window.source_sha256 != _sha(plan.source)
                or not isinstance(result, ParsedWindow)
                or type(result.index) is not int or result.index != window.index
                or result.window_hash != fingerprint(window.evidence())
                or fingerprint(parser(window, result.raw_output).evidence()) != fingerprint(result.evidence())):
            raise AgentError("TRANSLATION_CHUNK_STITCH_INVALID", 409)
        output.append(window.prefix + result.core_translation + window.suffix)
        cursor = window.core.end
    if cursor != len(plan.source):
        raise AgentError("TRANSLATION_CHUNK_STITCH_INVALID", 409)
    return "".join(output)
