"""Dedicated EN->RU translation; independent editorial gates remain mandatory."""

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import AgentError

MADLAD_MODEL = "madlad400-3b-q4k-causal-v2"
MADLAD_BACKEND_CONTRACT = "madlad400-candle-causal-v2"
MADLAD_SEGMENTATION_CONTRACT = "madlad-segment-budget-v1"
MADLAD_ARTIFACT_SHA256 = {
    "worker": "7b3fd9c77e130992a9b233c30b26a0245b9bb4386a5ecc7250f244d90d4e97cd",
    "model": "ea6e5531a3e95213c7f0635988d119e078a655c09306e47851e15d4c0c3f9c37",
    "tokenizer": "a2799ccc696b752ba00c34f58726bfe253a04921ceb6cfc620400f560474790b",
    "config": "cad399cab799b99409a6ec2d90d72552257c2bb752861261d2016691e0643e7c",
}
AUTOMATIC_TRANSLATION_MODELS = frozenset({"hy-mt2-7b-q8", MADLAD_MODEL})
MODELS = {"hy-mt2-1.8b-q8", "hy-mt2-7b-q8", "translategemma-12b-q4", "opus-mt-en-ru-int8", MADLAD_MODEL}
_NUMBER = re.compile(r"(?P<sign>[+\-−][ \t]*)?(?P<number>\d+(?:(?:[.,]\d+)|(?:[ \u00a0\u202f]\d{3}))*)")
_SPACE = r"[ \u00a0\u202f]"


def _numeric_tokens(text):
    # Keep alphanumeric identifiers (including L2, ID-01 and attached units) exact.
    identifiers = [match for match in re.finditer(r"\w+(?:[-.]\w+)*", text)
                   if any(char.isalpha() or char == '_' for char in match[0])
                   and any(char.isdigit() for char in match[0])]
    protected = Counter(match[0] for match in identifiers)
    tokens = []
    for match in _NUMBER.finditer(text):
        start, end = match.span('number')
        if any(start < identifier.end() and end > identifier.start() for identifier in identifiers):
            continue
        # Dates, ranges, leading decimal separators and malformed partial tokens are
        # outside this formatter. Leave the complete segment for the existing gate.
        if ((start and text[start - 1] in '.,')
                or (end + 1 < len(text) and text[end] in '-–—−' and text[end + 1].isdigit())):
            return None, None
        if tokens and text[tokens[-1].end('number'):match.start()].isspace():
            return None, None  # A whitespace-only numeric list is ambiguous grouping.
        tokens.append(match)
    return tokens, protected


def _decimal_spellings(raw, english):
    """Possible exact positive values; never infer a unit conversion or use floats."""
    normalized = re.sub(_SPACE, '', raw)
    integer = re.split(r'[.,]', normalized)[0]
    if len(integer) > 1 and integer.startswith('0'):
        return set()  # Leading-zero codes/dates are not ordinary numeric typography.
    if re.search(_SPACE, raw):
        if not re.fullmatch(rf"\d{{1,3}}(?:{_SPACE}\d{{3}})+(?:[.,]\d+)?", raw):
            return set()
        if english and ',' in normalized:
            return set()
        return {Decimal(normalized.replace(',', '.'))}
    values = set()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        values.add(Decimal(raw))
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        values.add(Decimal(raw.replace(',', '')))
    if not english:
        if re.fullmatch(r"\d+,\d+", raw):
            values.add(Decimal(raw.replace(',', '.')))
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", raw):
            values.add(Decimal(raw.replace('.', '').replace(',', '.')))
    return values


def restore_numeric_spelling(original, translated):
    """Return (text, count) for uniquely equivalent numeric typography only.

    This preserves the old raw-spelling gate. It makes no judgement about semantic
    fidelity: words, units, dates, order and sign characters are never rewritten.
    Any missing/extra value, ambiguous interpretation or source spelling disables
    every replacement for the segment.
    """
    source, source_ids = _numeric_tokens(original)
    target, target_ids = _numeric_tokens(translated)
    if source is None or target is None or source_ids != target_ids or len(source) != len(target):
        return translated, 0
    spellings, forms, required = {}, defaultdict(set), Counter()

    def key(value, match):
        sign = (match['sign'] or '').strip()
        category = '-' if sign in {'-', '−'} else sign
        return (value.copy_negate() if category == '-' else value), category

    try:
        for match in source:
            values = _decimal_spellings(match['number'], english=True)
            if len(values) != 1:
                return translated, 0
            value = key(next(iter(values)), match)
            forms[value[0]].add(match[0])
            if len(forms[value[0]]) != 1:
                return translated, 0
            spellings[value] = match['number']
            required[value] += 1
        replacements, observed = [], Counter()
        for match in target:
            values = _decimal_spellings(match['number'], english=False)
            candidates = {key(value, match) for value in values}
            if len(values) > 1:
                # Never explain away a possible changed value with source context.
                # An unchanged exact source lexeme is the only ambiguity lock.
                candidates = {value for value in candidates if value in required
                              and spellings[value] == match['number']}
            else:
                candidates.intersection_update(required)
            if len(candidates) != 1:
                return translated, 0
            value = next(iter(candidates))
            observed[value] += 1
            replacements.append((match, spellings[value]))
        if observed != required:
            return translated, 0
    except InvalidOperation:
        return translated, 0
    output, cursor, count = [], 0, 0
    for match, spelling in replacements:
        start, end = match.span('number')
        output.extend((translated[cursor:start], spelling))
        cursor = end
        count += match['number'] != spelling
    output.append(translated[cursor:])
    return ''.join(output), count


def segments(text, limit=1400):
    """Bound inputs without dropping tails; preserve paragraph/line separators."""
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise AgentError("TRANSLATION_INPUT_INVALID")
    result = []
    for paragraph in re.split(r"(\n+)", text):
        if not paragraph:
            continue
        if paragraph.isspace():
            result.append((paragraph, False))
            continue
        while len(paragraph) > limit:
            # Prefer complete sentences; otherwise retain all words in bounded chunks.
            matches = list(re.finditer(r"[.!?][\"']?\s+", paragraph[:limit]))
            cut = matches[-1].end() if matches else paragraph.rfind(" ", 0, limit)
            if cut < limit // 3:
                raise AgentError("TRANSLATION_UNSPLITTABLE_INPUT")
            result.append((paragraph[:cut].strip(), True))
            result.append((" ", False))
            paragraph = paragraph[cut:].lstrip()
        if paragraph:
            result.append((paragraph, True))
    return result


def translation_messages(model, text, prompt):
    # MADLAD — encoder-decoder T5 с собственным Candle worker, а не chat backend.
    if model not in MODELS - {"opus-mt-en-ru-int8", MADLAD_MODEL}:
        raise AgentError("TRANSLATION_MODEL_INVALID")
    if model == "translategemma-12b-q4":
        return [{"role": "user", "content": [{"type": "text", "source_lang_code": "en",
                "target_lang_code": "ru", "text": text}]}]
    return [{"role": "user", "content": prompt.replace("{source_text}", text)}]


class DedicatedTranslator:
    """Compatible with ArticleQuality's generate_text interface, without tool access."""

    immutable_candidates = True

    def __init__(self, config, transport=None):
        if not isinstance(config, dict):
            raise AgentError("TRANSLATION_CONFIG_INVALID")
        self.model = config.get("model")
        self.segment_chars = config.get("segment_chars", 1400)
        url = config.get("base_url", "")
        if not isinstance(self.model, str) or not isinstance(url, str):
            raise AgentError("TRANSLATION_CONFIG_INVALID")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise AgentError("TRANSLATION_CONFIG_INVALID") from exc
        if (self.model not in MODELS or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or port == 0):
            raise AgentError("TRANSLATION_CONFIG_INVALID")
        if (type(self.segment_chars) is not int or not 400 <= self.segment_chars <= 1400
                or self.segment_chars != 1400 and self.model != MADLAD_MODEL):
            raise AgentError("TRANSLATION_CONFIG_INVALID")
        self.url = url.rstrip("/")
        self.client = httpx.Client(timeout=600, trust_env=False, transport=transport,
                                   follow_redirects=False)
        self._cached_source = self._cached_result = None

    def translate(self, text):
        output, metrics = [], []
        for part, translate in segments(text, limit=self.segment_chars):
            if not translate:
                output.append(part)
                continue
            try:
                response = self.client.post(self.url + "/translate", json={
                    "model": self.model, "text": part, "source": "en", "target": "ru"})
                response.raise_for_status()
                value = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise AgentError("TRANSLATION_SERVICE_UNAVAILABLE", 503) from exc
            if (not isinstance(value, dict)
                    or value.get("model") != self.model or value.get("complete") is not True
                    or not isinstance(value.get("text"), str) or not value["text"].strip()):
                raise AgentError("TRANSLATION_RESPONSE_INVALID", 503)
            segment_metrics = value.get("metrics", {})
            # Один alias не отличает исправленную сборку от прежнего T5 backend.
            # Сверяем закреплённые артефакты каждого сегмента до форматирования.
            if self.model == MADLAD_MODEL and (
                    not isinstance(segment_metrics, dict)
                    or segment_metrics.get("backend_contract") != MADLAD_BACKEND_CONTRACT
                    or segment_metrics.get("artifact_sha256") != MADLAD_ARTIFACT_SHA256):
                raise AgentError("TRANSLATION_RESPONSE_INVALID", 503)
            restored, count = restore_numeric_spelling(part, value["text"].strip())
            output.append(restored)
            metadata = {**(segment_metrics if isinstance(segment_metrics, dict) else {}),
                        "numeric_spelling_restored": count}
            if self.segment_chars != 1400:
                metadata.update(segmentation_contract=MADLAD_SEGMENTATION_CONTRACT,
                    segment_chars=self.segment_chars, segment_index=len(metrics),
                    segment_input_sha256=hashlib.sha256(part.encode("utf-8")).hexdigest(),
                    segment_output_sha256=hashlib.sha256(restored.encode("utf-8")).hexdigest(),
                    segment_input_chars=len(part), segment_output_chars=len(restored),
                    segment_raw_output_sha256=hashlib.sha256(value["text"].encode("utf-8")).hexdigest(),
                    segment_raw_output_chars=len(value["text"]))
            metrics.append(metadata)
        return "".join(output), metrics

    def generate_text(self, prompt):
        try:
            source = json.loads(prompt)["source_untrusted"]
            title, body = source["title"], source["body"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError("TRANSLATION_INPUT_INVALID") from exc
        if not isinstance(title, str) or not isinstance(body, str):
            raise AgentError("TRANSLATION_INPUT_INVALID")
        cache_key = (title, body)
        if cache_key == self._cached_source:
            translated, info = copy.deepcopy(self._cached_result)
            return translated, {**info, "cache_reused": True}
        # A translation-only model cannot edit against reviewer feedback. Reuse the exact
        # candidate within this instance; ArticleQuality retains its independent refusal.
        self._cached_source = self._cached_result = None
        translated_title, title_metrics = self.translate(title)
        translated_body, body_metrics = self.translate(body)
        info = {"model": self.model, "segments": title_metrics + body_metrics, "cache_reused": False}
        if self.segment_chars != 1400:
            bound = []
            for field, values in (("title", title_metrics), ("body", body_metrics)):
                for field_index, metadata in enumerate(values):
                    bound.append({**metadata, "segment_field": field, "field_segment_index": field_index,
                                  "segment_index": len(bound)})
            info.update(segmentation_contract=MADLAD_SEGMENTATION_CONTRACT,
                        segment_chars=self.segment_chars, segments=bound)
        result = (json.dumps({"title": translated_title, "body": translated_body}, ensure_ascii=False), info)
        self._cached_source, self._cached_result = cache_key, copy.deepcopy(result)
        return result


def configured_translator(settings):
    """Absent/disabled config preserves the existing OpenCode translator."""
    path = Path(settings.root) / "config" / "translation.json"
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
            raise ValueError
    except (ValueError, OSError) as exc:
        raise AgentError("TRANSLATION_CONFIG_INVALID") from exc
    return DedicatedTranslator(config) if config["enabled"] else None
