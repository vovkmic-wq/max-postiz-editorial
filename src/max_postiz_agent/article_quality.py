"""Bounded local translation and independent checks for supplied English articles.

This module does not fetch an article or establish permission to republish it.
The caller must validate the source and its rights before passing its text here.
"""

import copy
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from .article_attribution import article_footer
from .article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT, reference_urls
from .adapters import extract_object
from .collectors import detect_language
from .config import AgentError, fingerprint
from .editorial import numbers, translation_gate
from .network import resemblances_ip

SPELLING_URL = "http://127.0.0.1:8010/v2/check"
TOPICS = {"science", "stories", "humor"}
MAGELLANIC_EVIDENCE = "https://www.astronet.ru/db/msg/1167613/lmc.html"
MULTISCALE_EVIDENCE = "https://www.mathnet.ru/php/archive.phtml?jrnid=vmp&option_lang=rus&paperid=623&wshow=paper"
MULTISCALE_SOURCE_TERM = "Multi-scale Ultra-high Resolution (MUR) Sea Surface Temperature Analysis"
MULTISCALE_PHRASE = "многомасштабного анализа"
RONI_EVIDENCE = "https://public.wmo.int/ru/temy/yavleniya-el-nino-i-la-ninya"
RONI_SOURCE_TERM = "Relative Oceanic Niño Index (RONI)"
SPACETIME_EVIDENCE = "https://www.mathnet.ru/tmf6729"
SPACETIME_SOURCE_CONTEXT = "where space-time is dragged along with the black hole as it turns."
SPACETIME_SOURCE_TERM = "extreme mass ratio inspirals"
FRESH_SPACETIME_SPELLING_CONTRACT = "spacetime-fresh-review-v1"
_SPACETIME_PHRASE = re.compile(r"(?<![\w-])в которой (?P<word>пространство-время) "
                               r"уносится вместе с чёрной дырой по мере её вращения(?![\w-])")
_RONI_PHRASE = re.compile(r"(?<![\w-])(?:[Оо]тносительного океанического индекса|"
                          r"[Оо]тносительный океанический индекс) (?P<word>Ниньо) \(RONI\)(?![\w-])")
MAX_REVIEW_ISSUES = 3
MAX_REVIEW_ISSUE_CHARS = 1024
MAX_REVIEW_WARNINGS = 8
MAX_REVIEW_WARNINGS_BYTES = 8192
REVIEW_PROTOCOL_CONTRACT = "meaning-first-advisory-v2"
MAX_PROTOCOL_REVIEW_BYTES = 65536
LEGACY_REVIEW_RUBRIC = "legacy"
MEANING_FIRST_REVIEW_RUBRIC = "meaning-first-v1"
REVIEW_RUBRICS = {LEGACY_REVIEW_RUBRIC, MEANING_FIRST_REVIEW_RUBRIC}
_INLINE_ARTICLE_LINK = re.compile(r"(?<![!\\])\[([^\[\]\r\n]+)\]\((https?://)")


def normalize_article_links(text):
    """Keep inline link labels readable and put each literal URL on its own line.

    MAX's plain-text paste does not interpret Markdown. Only explicit inline
    links are changed; bare URLs, surrounding parentheses and images stay exact.
    Balanced parentheses inside a destination remain part of the literal URL.
    """
    parts, destinations, cursor = [], [], 0
    for match in _INLINE_ARTICLE_LINK.finditer(text):
        if match.start() < cursor:
            continue
        depth, end = 0, match.end()
        while end < len(text):
            char = text[end]
            if char.isspace() or char in '<>"[]\\':
                break
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    parts.extend((text[cursor:match.start()], match.group(1)))
                    destinations.append(text[match.start(2):end])
                    cursor = end + 1
                    break
                depth -= 1
            end += 1
    if not destinations:
        return text
    parts.append(text[cursor:])
    return "".join(parts).rstrip() + "\n\n" + "\n".join(destinations)


def _normalize_review(value):
    """Unwrap one unambiguous issue envelope without changing the verdict."""
    if (not isinstance(value, dict) or set(value) != {"approved", "topic", "issues"}
            or type(value["approved"]) is not bool
            or not isinstance(value["topic"], str)
            or not isinstance(value["issues"], list)
            or len(value["issues"]) > MAX_REVIEW_ISSUES):
        raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID")
    issues = []
    for issue in value["issues"]:
        if isinstance(issue, dict) and set(issue) == {"issue"}:
            issue = issue["issue"]
        if (not isinstance(issue, str) or not issue.strip()
                or len(issue) > MAX_REVIEW_ISSUE_CHARS):
            raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID")
        # Preserve every character of a negative finding; do not truncate or filter it.
        issues.append(issue)
    return {"approved": value["approved"], "topic": value["topic"], "issues": issues}


def normalize_rubric_review(value, rubric):
    """Separate advisory wording from substantive issues without changing a verdict."""
    if rubric == LEGACY_REVIEW_RUBRIC:
        return _normalize_review(value)
    if rubric != MEANING_FIRST_REVIEW_RUBRIC:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
    if not isinstance(value, dict) or set(value) != {"approved", "topic", "issues", "warnings"}:
        raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID")
    normalized = _normalize_review({key: value[key] for key in ("approved", "topic", "issues")})
    warnings = value["warnings"]
    if (not isinstance(warnings, list) or len(warnings) > MAX_REVIEW_WARNINGS
            or any(not isinstance(item, str) or not item.strip()
                   or len(item) > MAX_REVIEW_ISSUE_CHARS for item in warnings)):
        raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID")
    try:
        if sum(len(item.encode("utf-8")) for item in warnings) > MAX_REVIEW_WARNINGS_BYTES:
            raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID")
    except UnicodeError as exc:
        raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID") from exc
    return {**normalized, "warnings": copy.deepcopy(warnings)}


def protocol_recovery_review(raw_review):
    """Classify only a typed positive whose old sole defect was advisory count."""
    error = "ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"
    try:
        if (not isinstance(raw_review, str) or not raw_review.strip()
                or len(raw_review.encode("utf-8")) > MAX_PROTOCOL_REVIEW_BYTES):
            raise AgentError(error, 409)
        reviewed = normalize_rubric_review(extract_object(raw_review), MEANING_FIRST_REVIEW_RUBRIC)
        if (reviewed["approved"] is not True or reviewed["topic"] not in TOPICS
                or reviewed["issues"] or not MAX_REVIEW_ISSUES < len(reviewed["warnings"]) <= MAX_REVIEW_WARNINGS):
            raise AgentError(error, 409)
        return reviewed
    except (AgentError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise AgentError(error, 409) from exc


def review_parser_contract(raw_review):
    """Bind the exact raw response to the bounded advisory parser contract."""
    limits = {"contract": REVIEW_PROTOCOL_CONTRACT, "max_issues": MAX_REVIEW_ISSUES,
              "max_issue_chars": MAX_REVIEW_ISSUE_CHARS, "max_warnings": MAX_REVIEW_WARNINGS,
              "max_warning_chars": MAX_REVIEW_ISSUE_CHARS,
              "max_warnings_utf8_bytes": MAX_REVIEW_WARNINGS_BYTES}
    return {**limits, "contract_hash": fingerprint(limits),
            "raw_review_sha256": hashlib.sha256(raw_review.encode("utf-8")).hexdigest()}


def protocol_recovery_provenance(raw_review, review_prompt_hash):
    """Describe only a recognized positive count overflow, without changing it."""
    reviewed = protocol_recovery_review(raw_review)
    if not isinstance(review_prompt_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", review_prompt_hash):
        raise AgentError("ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE", 409)
    parser = review_parser_contract(raw_review)
    return {"contract": REVIEW_PROTOCOL_CONTRACT, "parser_contract_hash": parser["contract_hash"],
            "legacy_error": "ARTICLE_REVIEW_SCHEMA_INVALID", "reason": "ADVISORY_COUNT_ONLY_OVERFLOW",
            "legacy_warning_limit": MAX_REVIEW_ISSUES, "warning_limit": MAX_REVIEW_WARNINGS,
            "warnings_utf8_limit": MAX_REVIEW_WARNINGS_BYTES, "raw_utf8_limit": MAX_PROTOCOL_REVIEW_BYTES,
            "warning_count": len(reviewed["warnings"]), "raw_review_sha256": parser["raw_review_sha256"],
            "review_rubric": MEANING_FIRST_REVIEW_RUBRIC, "review_prompt_hash": review_prompt_hash}


def article_reference_urls(text):
    """Исторический V1: сохранён для точного воспроизведения старых traces."""
    return reference_urls(text, contract=LEGACY_REFERENCE_CONTRACT)


def rubric_translation_gate(original, translated, rubric, *, reference_contract=LEGACY_REFERENCE_CONTRACT):
    """Keep numerical/language constraints and preserve every embedded reference."""
    if not isinstance(rubric, str) or rubric not in REVIEW_RUBRICS:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
    source_urls = reference_urls(original, contract=reference_contract)
    candidate_urls = reference_urls(translated, contract=reference_contract)
    translation_gate(original, translated)
    if (rubric == MEANING_FIRST_REVIEW_RUBRIC
            and source_urls != candidate_urls):
        raise AgentError("TRANSLATION_REFERENCE_CHANGED")


def article_review_context(instruction, source, translation, rubric, prompt_hash,
                           *, reference_contract=LEGACY_REFERENCE_CONTRACT):
    """Старый контекст неизменен; V2 явно связывает диагностику с literal URL.

    Manifest вычисляется из тех же EN/RU, которые проверяет hard gate. Он не
    меняет текст и не является самостоятельным подтверждением качества.
    """
    if not isinstance(rubric, str) or rubric not in REVIEW_RUBRICS:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
    source_urls = reference_urls(source, contract=reference_contract)
    candidate_urls = reference_urls(translation, contract=reference_contract)
    context = {"instruction": instruction, "source_untrusted": source,
               "translation_untrusted": translation}
    if rubric != LEGACY_REVIEW_RUBRIC:
        context.update(review_rubric=rubric, review_prompt_hash=prompt_hash)
    if reference_contract == REFERENCE_CONTRACT:
        context["reference_check"] = {"contract": reference_contract,
            "source_urls": sorted(source_urls), "candidate_urls": sorted(candidate_urls)}
    return context


def _numeric_feedback(original, translated):
    source, candidate = numbers(original), numbers(translated)
    unexpected = candidate - source
    notation_repairs = []
    for observed in sorted(unexpected):
        # A diagnostic suggestion only. The gate still compares the exact raw tokens.
        possible = [token for token in source
                    if token.replace(",", ".") == observed.replace(",", ".")]
        if len(possible) == 1:
            notation_repairs.append({"observed": observed, "required": possible[0]})
    return {"code": "TRANSLATION_NUMBER_CHANGED",
            "original_numbers": sorted(source), "translated_numbers": sorted(candidate),
            "missing_numbers": sorted(source - candidate),
            "unexpected_numbers": sorted(unexpected), "notation_repairs": notation_repairs,
            "advice": "Copy every numeric token character-for-character from the source, including decimal "
                      "dots or commas and zero padding, even when Russian typography normally differs. "
                      "Do not convert units, times or number formats; translate their qualifiers. "
                      "Replace unexpected notation in every occurrence, including identifiers. "
                      "Do not merely add the source notation beside a localized copy."}


def multiscale_spelling_evidence(original, translated, match):
    """Resolve one exact source-bound MUR term, never a word-prefix allowlist."""
    rule, kind = match.get("rule"), match.get("type")
    offset, length = match.get("offset"), match.get("length")
    if (not isinstance(rule, dict) or rule.get("id") != "MORFOLOGIK_RULE_RU_RU"
            or rule.get("issueType") != "misspelling"
            or not isinstance(kind, dict) or kind.get("typeName") != "UnknownWord"
            or type(offset) is not int or offset < 0 or type(length) is not int
            or length != len("многомасштабного")
            or not re.search(r"(?<![A-Za-z])" + re.escape(MULTISCALE_SOURCE_TERM) + r"(?![A-Za-z])", original)):
        return None
    for phrase in re.finditer(r"(?<![\w-])" + MULTISCALE_PHRASE + r"(?![\w-])", translated):
        try:
            expected = len(translated[:phrase.start()].encode("utf-16-le")) // 2
        except UnicodeError:
            continue
        if offset == expected:
            return {"match": copy.deepcopy(match), "word": "многомасштабного", "phrase": phrase.group(),
                    "source_term": MULTISCALE_SOURCE_TERM, "evidence_url": MULTISCALE_EVIDENCE,
                    "reason": "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM"}
    return None


def scientific_spelling_review(raw_review):
    """Require a typed positive saved verdict; provenance is bound by the caller."""
    error = "ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"
    try:
        if (not isinstance(raw_review, str) or not raw_review.strip()
                or len(raw_review.encode("utf-8")) > MAX_PROTOCOL_REVIEW_BYTES):
            raise AgentError(error, 409)
        reviewed = normalize_rubric_review(extract_object(raw_review), MEANING_FIRST_REVIEW_RUBRIC)
        if reviewed["approved"] is not True or reviewed["issues"] or reviewed["topic"] not in TOPICS:
            raise AgentError(error, 409)
        return reviewed
    except (AgentError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise AgentError(error, 409) from exc


def roni_spelling_evidence(original, translated, match):
    """Recognize only the documented Niño token inside the full RONI term."""
    if not isinstance(original, str) or not isinstance(translated, str) or not isinstance(match, dict):
        return None
    rule, kind = match.get("rule"), match.get("type")
    offset, length = match.get("offset"), match.get("length")
    if (not isinstance(rule, dict) or rule.get("id") != "MORFOLOGIK_RULE_RU_RU"
            or rule.get("issueType") != "misspelling"
            or not isinstance(kind, dict) or kind.get("typeName") != "UnknownWord"
            or type(offset) is not int or offset < 0 or type(length) is not int or length != len("Ниньо")
            or not re.search(r"(?<![\w-])" + re.escape(RONI_SOURCE_TERM) + r"(?![\w-])", original)):
        return None
    for phrase in _RONI_PHRASE.finditer(translated):
        try:
            expected = len(translated[:phrase.start("word")].encode("utf-16-le")) // 2
        except UnicodeError:
            continue
        if offset == expected:
            return {"match": copy.deepcopy(match), "word": "Ниньо", "phrase": phrase.group(),
                    "source_term": RONI_SOURCE_TERM, "evidence_url": RONI_EVIDENCE,
                    "reason": "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM"}
    return None


def spacetime_spelling_evidence(original, translated, match):
    """Resolve one exact scientific token in the verified EMRI source context."""
    if not isinstance(original, str) or not isinstance(translated, str) or not isinstance(match, dict):
        return None
    rule, kind = match.get("rule"), match.get("type")
    offset, length = match.get("offset"), match.get("length")
    if (not isinstance(rule, dict) or rule.get("id") != "MORFOLOGIK_RULE_RU_RU"
            or rule.get("issueType") != "misspelling" or not isinstance(rule.get("category"), dict)
            or rule["category"].get("id") != "TYPOS"
            or not isinstance(kind, dict) or kind.get("typeName") != "UnknownWord"
            or type(offset) is not int or offset < 0 or type(length) is not int or length != len("пространство-время")
            or any(not re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", original)
                   for term in (SPACETIME_SOURCE_CONTEXT, SPACETIME_SOURCE_TERM))):
        return None
    for phrase in _SPACETIME_PHRASE.finditer(translated):
        try:
            expected = len(translated[:phrase.start("word")].encode("utf-16-le")) // 2
        except UnicodeError:
            continue
        if offset == expected:
            return {"match": copy.deepcopy(match), "word": "пространство-время", "phrase": phrase.group(),
                    "source_term": SPACETIME_SOURCE_TERM, "source_context": SPACETIME_SOURCE_CONTEXT,
                    "evidence_url": SPACETIME_EVIDENCE, "reason": "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM"}
    return None


def adjudicate_scientific_spelling(original, translated, matches, *, allow_spacetime=False):
    """Resolve documented exact scientific terms; retain every other LT finding."""
    adjudicated, unresolved = [], []
    for match in matches:
        rule, kind = match.get("rule"), match.get("type")
        offset, length = match.get("offset"), match.get("length")
        evidence = (multiscale_spelling_evidence(original, translated, match)
                    or roni_spelling_evidence(original, translated, match)
                    or (spacetime_spelling_evidence(original, translated, match)
                        if allow_spacetime is True else None))
        if (isinstance(rule, dict) and rule.get("id") == "MORFOLOGIK_RULE_RU_RU"
                and rule.get("issueType") == "misspelling"
                and isinstance(kind, dict) and kind.get("typeName") == "UnknownWord"
                and type(offset) is int and offset >= 0 and type(length) is int
                and length == len("Магелланово")):
            for phrase in re.finditer(r"(?<![\w-])(Большое|Малое) Магелланово облако(?![\w-])", translated):
                english = "Large" if phrase.group(1) == "Большое" else "Small"
                word_start = phrase.start() + len(phrase.group(1)) + 1
                try:
                    # LanguageTool offsets count Java UTF-16 code units.
                    expected = len(translated[:word_start].encode("utf-16-le")) // 2
                except UnicodeError:
                    continue
                if offset == expected and re.search(
                        rf"(?<![A-Za-z]){english}\s+Magellanic\s+Cloud(?![A-Za-z])", original,
                        flags=re.IGNORECASE):
                    evidence = {"match": copy.deepcopy(match), "word": "Магелланово",
                        "phrase": phrase.group(), "source_term": english + " Magellanic Cloud",
                        "evidence_url": MAGELLANIC_EVIDENCE,
                        "reason": "SOURCE_BOUND_VERIFIED_SCIENTIFIC_NAME"}
                    break
        if evidence:
            adjudicated.append(evidence)
        else:
            unresolved.append(match)
    return adjudicated, unresolved


def rubric_spelling(original, translated, matches, rubric, *, allow_spacetime=False):
    """Retain style suggestions as audited advice; grammar and unknown rules block."""
    if not isinstance(rubric, str) or rubric not in REVIEW_RUBRICS:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
    adjudicated, unresolved = adjudicate_scientific_spelling(original, translated, matches,
                                                           allow_spacetime=allow_spacetime)
    if rubric == LEGACY_REVIEW_RUBRIC:
        return adjudicated, unresolved
    blocking = []
    for match in unresolved:
        rule, kind = match.get("rule"), match.get("type")
        if (isinstance(rule, dict) and rule.get("issueType") in {"style", "register"}
                and isinstance(kind, dict) and kind.get("typeName") in {"Other", "Style", "Register"}
                and isinstance(rule.get("category"), dict)
                and isinstance(rule["category"].get("id"), str) and rule["category"]["id"]
                and rule["category"].get("id") not in {"GRAMMAR", "TYPOS", "CONFUSED_WORDS"}):
            adjudicated.append({"match": copy.deepcopy(match), "reason": "NON_BLOCKING_STYLE_RULE",
                                "review_rubric": rubric})
        else:
            blocking.append(match)
    return adjudicated, blocking


def canonical_article_url(value):
    """Validate a public HTTPS reference without resolving or requesting its host."""
    try:
        if (not isinstance(value, str) or not value.strip() or len(value) > 2000
                or re.search(r"[\x00-\x20\x7f\\]", value.strip())):
            raise ValueError
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in {None, 443}):
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (resemblances_ip(host) or "." not in host or len(host) > 253
                    or host.endswith((".local", ".localhost", ".internal", ".lan"))
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split("."))):
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
            if address.version == 6:
                host = "[" + address.compressed + "]"
        if re.search(r"[\x00-\x1f\x7f\\]", unquote(parsed.path + parsed.query)):
            raise ValueError
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise AgentError("ARTICLE_URL_INVALID") from exc


def _validated_source(title, original):
    if not isinstance(title, str) or not isinstance(original, str):
        raise AgentError("ARTICLE_INPUT_INVALID")
    title, original = title.strip(), original.strip()
    source = title + "\n\n" + original
    if not 3 <= len(title) <= 500 or len(original) < 80 or len(source) > 9000:
        raise AgentError("ARTICLE_INPUT_SIZE_INVALID")
    if detect_language(source) != "en" or len(re.findall(r"[A-Za-z]{2,}", source)) < 12:
        raise AgentError("ARTICLE_ENGLISH_SOURCE_REQUIRED")
    return title, original, source


class ArticleQuality:
    def __init__(self, translator, reviewer, spelling_url=SPELLING_URL, transport=None,
                 prompt_dir=None, trace=None, allow_human_rejection=False,
                 review_rubric=LEGACY_REVIEW_RUBRIC, reference_contract=None):
        if (translator is reviewer or (
                getattr(translator, "model", None) and getattr(reviewer, "model", None)
                and translator.model == reviewer.model)):
            raise AgentError("ARTICLE_INDEPENDENT_REVIEW_REQUIRED")
        if spelling_url != SPELLING_URL:
            raise AgentError("LOCAL_SPELLING_REQUIRED")
        if trace is not None and not callable(trace):
            raise AgentError("ARTICLE_TRACE_INVALID")
        self.translator, self.reviewer = translator, reviewer
        self.trace = trace
        if type(allow_human_rejection) is not bool:
            raise AgentError("ARTICLE_REVIEW_MODE_INVALID")
        self.allow_human_rejection = allow_human_rejection
        if not isinstance(review_rubric, str) or review_rubric not in REVIEW_RUBRICS:
            raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
        self.review_rubric = review_rubric
        if reference_contract is None:
            reference_contract = (REFERENCE_CONTRACT if review_rubric == MEANING_FIRST_REVIEW_RUBRIC
                                  else LEGACY_REFERENCE_CONTRACT)
        # Исторический cycle передаёт V1 явно: обновление кода не меняет replay.
        reference_urls("", contract=reference_contract)
        self.reference_contract = reference_contract
        prompts = Path(prompt_dir) if prompt_dir else Path(__file__).resolve().parents[2] / "prompts"
        self.translation_prompt = (prompts / "ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8")
        review_file = ("ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
                       if review_rubric == MEANING_FIRST_REVIEW_RUBRIC else "ARTICLE_REVIEW.txt")
        self.review_prompt = (prompts / review_file).read_text(encoding="utf-8")
        self.review_prompt_hash = hashlib.sha256(self.review_prompt.encode()).hexdigest()
        self.spelling_url = spelling_url
        self.spelling = httpx.Client(timeout=60, trust_env=False, transport=transport)

    def _emit(self, phase, attempt, **value):
        if self.trace is not None:
            try:
                # A diagnostic observer cannot mutate the text being checked.
                self.trace(copy.deepcopy({"phase": phase, "attempt": attempt, **value}))
            except Exception as exc:
                raise AgentError("ARTICLE_TRACE_FAILED", 503) from exc

    def _spelling_matches(self, text):
        try:
            response = self.spelling.post(self.spelling_url, data={"language": "ru-RU", "text": text})
            response.raise_for_status()
            matches = response.json()["matches"]
            if not isinstance(matches, list) or any(not isinstance(x, dict) for x in matches):
                raise TypeError
            return matches
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise AgentError("ARTICLE_SPELLING_UNAVAILABLE", 503) from exc

    def _human_evidence(self, prepared, title, original, reviewed, raw, matches,
                        adjudicated, unresolved, *, fresh_spelling=False):
        if self.allow_human_rejection or self.review_rubric == MEANING_FIRST_REVIEW_RUBRIC:
            prepared["machine_review_status"] = "passed" if reviewed["approved"] else "rejected"
            prepared["machine_review_evidence"] = {
                "review_raw": raw, "review": copy.deepcopy(reviewed),
                "spelling_matches": copy.deepcopy(matches),
                "spelling_adjudicated": copy.deepcopy(adjudicated),
                "spelling_unresolved": copy.deepcopy(unresolved), "hard_gates_passed": True,
                "text_hash": fingerprint({"title": prepared["title"], "body": prepared["body"]}),
                "source_hash": fingerprint({"title": title, "body": original, "url": prepared["url"]})}
            if self.review_rubric == MEANING_FIRST_REVIEW_RUBRIC:
                prepared.update(review_rubric=self.review_rubric,
                                review_prompt_hash=self.review_prompt_hash,
                                warnings=copy.deepcopy(reviewed["warnings"]))
                prepared["machine_review_evidence"].update(
                    review_rubric=self.review_rubric, review_prompt_hash=self.review_prompt_hash,
                    parser_contract=review_parser_contract(raw))
                if fresh_spelling is True:
                    prepared["machine_review_evidence"]["fresh_spelling_contract"] = FRESH_SPACETIME_SPELLING_CONTRACT
            if self.reference_contract == REFERENCE_CONTRACT:
                prepared["reference_contract"] = self.reference_contract
                prepared["machine_review_evidence"]["reference_check"] = article_review_context(
                    self.review_prompt, title + "\n\n" + original, prepared["translation"],
                    self.review_rubric, self.review_prompt_hash,
                    reference_contract=self.reference_contract)["reference_check"]
        return prepared

    def prepare(self, title, original, url):
        url = canonical_article_url(url)
        title, original, source = _validated_source(title, original)
        context = {"instruction": self.translation_prompt,
                   "source_untrusted": {"title": title, "body": original, "url": url},
                   "numeric_tokens_must_match_exactly": sorted(numbers(source))}
        if self.reference_contract == REFERENCE_CONTRACT:
            context["reference_contract"] = self.reference_contract
        immutable_candidates = getattr(self.translator, "immutable_candidates", False) is True
        rejected_candidates = {}
        # Неизменяемый переводчик не исправляет feedback: повтор вернул бы тот же
        # отклонённый текст. Реальные редакторы сохраняют три попытки исправления.
        max_attempts = 1 if immutable_candidates else 3
        for attempt in range(1, max_attempts + 1):
            self._emit("translation_started", attempt, context=context)
            raw_translation = None
            try:
                returned = self.translator.generate_text(json.dumps(context, ensure_ascii=False))
                raw_translation = returned[0]
                # Новая явная сегментация сохраняет request/response bindings и
                # raw worker evidence в прежнем событии, не меняя порядок trace.
                # Старые модели/trace не получают выдуманное доказательство.
                metadata = (returned[1] if isinstance(returned, tuple) and len(returned) == 2
                            and isinstance(returned[1], dict) else {})
                evidence = ({"translation_metadata": metadata}
                            if metadata.get("segmentation_contract") else {})
                self._emit("translation_returned", attempt, raw=raw_translation, **evidence)
                translated = extract_object(raw_translation)
            except AgentError as exc:
                if exc.code != "INVALID_EDITOR_RESULT":
                    if exc.code != "ARTICLE_TRACE_FAILED":
                        self._emit("translation_failed", attempt, error=exc.code)
                    raise
                context["repair_feedback"] = "Return only valid JSON with title and body strings."
                self._emit("translation_invalid", attempt, error=exc.code, raw=raw_translation)
                self._emit("retry", attempt, feedback=context["repair_feedback"])
                continue
            if (set(translated) != {"title", "body"}
                    or any(not isinstance(v, str) or not v.strip() for v in translated.values())):
                context["repair_feedback"] = "Return exactly title and body as nonempty JSON strings."
                self._emit("translation_invalid", attempt, error="ARTICLE_TRANSLATION_SCHEMA_INVALID",
                           translation=translated)
                self._emit("retry", attempt, feedback=context["repair_feedback"])
                continue
            translated = {key: normalize_article_links(value.strip())
                          for key, value in translated.items()}
            self._emit("translation_parsed", attempt, translation=translated)
            text = translated["title"] + "\n\n" + translated["body"]
            full = text + "\n\n" + article_footer(url)
            context["previous_translation"] = translated
            try:
                rubric_translation_gate(source, text, self.review_rubric,
                                        reference_contract=self.reference_contract)
                if len(full) > 3500:
                    raise AgentError("ARTICLE_POST_TOO_LONG")
            except AgentError as exc:
                context["repair_feedback"] = (_numeric_feedback(source, text)
                    if exc.code == "TRANSLATION_NUMBER_CHANGED" else exc.code)
                self._emit("gate_rejected", attempt, error=exc.code, characters=len(full))
                self._emit("retry", attempt, feedback=context["repair_feedback"])
                continue
            self._emit("gate_passed", attempt, characters=len(full))
            if text in rejected_candidates:
                # A fresh translation may be reviewed; the same rejected text cannot
                # obtain approval by repeatedly sampling the independent reviewer.
                context["repair_feedback"] = copy.deepcopy(rejected_candidates[text])
                self._emit("candidate_repeated", attempt, feedback=context["repair_feedback"])
                self._emit("retry", attempt, feedback=context["repair_feedback"])
                continue
            review_context = article_review_context(self.review_prompt, source, text,
                self.review_rubric, self.review_prompt_hash, reference_contract=self.reference_contract)
            self._emit("review_started", attempt, context=review_context)
            raw_review = None
            try:
                raw_review = self.reviewer.generate_text(
                    json.dumps(review_context, ensure_ascii=False))[0]
                self._emit("review_returned", attempt, raw=raw_review)
                reviewed = extract_object(raw_review)
            except AgentError as exc:
                if exc.code != "INVALID_EDITOR_RESULT" or exc.status >= 500:
                    if exc.code != "ARTICLE_TRACE_FAILED":
                        self._emit("review_failed", attempt, error=exc.code)
                    raise
                context["repair_feedback"] = "Independent review returned invalid JSON; approval was not granted."
                if immutable_candidates:
                    context["repair_feedback"] = {"code": exc.code,
                        "raw_review_untrusted": raw_review,
                        "advice": context["repair_feedback"]}
                    rejected_candidates[text] = copy.deepcopy(context["repair_feedback"])
                self._emit("review_invalid", attempt, error=exc.code, raw=raw_review)
                self._emit("retry", attempt, feedback=context["repair_feedback"])
                continue
            try:
                normalized = normalize_rubric_review(reviewed, self.review_rubric)
            except AgentError:
                context["repair_feedback"] = {"code": "ARTICLE_REVIEW_SCHEMA_INVALID",
                    "review_untrusted": reviewed,
                    "advice": "Independent review schema invalid; approval was not granted. "
                              "Preserve any negative findings and correct the candidate against the source."}
                if immutable_candidates:
                    context["repair_feedback"]["raw_review_untrusted"] = raw_review
                if immutable_candidates or reviewed.get("approved") is False or reviewed.get("issues"):
                    rejected_candidates[text] = copy.deepcopy(context["repair_feedback"])
                self._emit("review_invalid", attempt, error="ARTICLE_REVIEW_SCHEMA_INVALID", review=reviewed)
                self._emit("retry", attempt, feedback=context["repair_feedback"])
                continue
            if normalized != reviewed:
                self._emit("review_normalized", attempt, raw_review=reviewed, review=normalized)
            reviewed = normalized
            self._emit("review_parsed", attempt, review=reviewed)
            self._emit("spelling_started", attempt)
            try:
                matches = self._spelling_matches(text)
            except AgentError as exc:
                self._emit("spelling_failed", attempt, error=exc.code)
                raise
            self._emit("spelling_returned", attempt, matches=matches)
            adjudicated, unresolved = rubric_spelling(source, text, matches, self.review_rubric,
                allow_spacetime=self.review_rubric == MEANING_FIRST_REVIEW_RUBRIC)
            self._emit("spelling_adjudicated", attempt, adjudicated=adjudicated, unresolved=unresolved)
            passed = reviewed["approved"] and not reviewed["issues"]
            human_rejection = (self.allow_human_rejection and reviewed["approved"] is False
                               and bool(reviewed["issues"]))
            if ((passed or human_rejection) and reviewed["topic"] in TOPICS and not unresolved):
                prepared = {"title": translated["title"], "body": full, "translation": text,
                            "original": source, "url": url, "topic": reviewed["topic"]}
                self._human_evidence(prepared, title, original, reviewed, raw_review, matches,
                                     adjudicated, unresolved, fresh_spelling=True)
                self._emit("accepted" if passed else "rejected_for_human", attempt, result=prepared)
                return prepared
            context["repair_feedback"] = {"review_approved": reviewed["approved"],
                "topic": reviewed["topic"], "review_issues": reviewed["issues"],
                "spelling_matches": unresolved[:20]}
            rejected_candidates[text] = copy.deepcopy(context["repair_feedback"])
            self._emit("retry", attempt, feedback=context["repair_feedback"])
        self._emit("rejected", attempt, error="ARTICLE_QUALITY_REJECTED",
                   feedback=context.get("repair_feedback"))
        raise AgentError("ARTICLE_QUALITY_REJECTED")

    def check_candidate(self, title, body, original_title, original, url):
        """Check an exact editorial correction without translating or publishing it."""
        url = canonical_article_url(url)
        original_title, original, source = _validated_source(original_title, original)
        if (not isinstance(title, str) or not title.strip()
                or not isinstance(body, str) or not body.strip()):
            raise AgentError("ARTICLE_TRANSLATION_INVALID")
        candidate_raw = {"title": title, "body": body}
        title, body = normalize_article_links(title.strip()), normalize_article_links(body.strip())
        text = title + "\n\n" + body
        full = text + "\n\n" + article_footer(url)
        self._emit("candidate_started", 1, mode="candidate", candidate={"title": title, "body": body},
                   source_untrusted={"title": original_title, "body": original, "url": url},
                   **({"reference_contract": self.reference_contract}
                      if self.reference_contract == REFERENCE_CONTRACT else {}),
                   **({"candidate_raw": candidate_raw}
                      if candidate_raw != {"title": title, "body": body} else {}))
        try:
            rubric_translation_gate(source, text, self.review_rubric,
                                    reference_contract=self.reference_contract)
            if len(full) > 3500:
                raise AgentError("ARTICLE_POST_TOO_LONG")
        except AgentError as exc:
            self._emit("gate_rejected", 1, error=exc.code, characters=len(full))
            self._emit("rejected", 1, mode="candidate", error=exc.code)
            raise
        self._emit("gate_passed", 1, characters=len(full))
        review_context = article_review_context(self.review_prompt, source, text,
            self.review_rubric, self.review_prompt_hash, reference_contract=self.reference_contract)
        self._emit("review_started", 1, context=review_context)
        raw_review = None
        try:
            raw_review = self.reviewer.generate_text(json.dumps(review_context, ensure_ascii=False))[0]
            self._emit("review_returned", 1, raw=raw_review)
            reviewed = extract_object(raw_review)
        except AgentError as exc:
            if exc.code != "INVALID_EDITOR_RESULT" or exc.status >= 500:
                if exc.code != "ARTICLE_TRACE_FAILED":
                    self._emit("review_failed", 1, error=exc.code)
                raise
            self._emit("review_invalid", 1, error=exc.code, raw=raw_review)
            self._emit("rejected", 1, mode="candidate", error="ARTICLE_QUALITY_REJECTED")
            raise AgentError("ARTICLE_QUALITY_REJECTED") from exc
        try:
            normalized = normalize_rubric_review(reviewed, self.review_rubric)
        except AgentError as exc:
            self._emit("review_invalid", 1, error="ARTICLE_REVIEW_SCHEMA_INVALID", review=reviewed)
            self._emit("rejected", 1, mode="candidate", error="ARTICLE_QUALITY_REJECTED")
            raise AgentError("ARTICLE_QUALITY_REJECTED") from exc
        if normalized != reviewed:
            self._emit("review_normalized", 1, raw_review=reviewed, review=normalized)
        reviewed = normalized
        self._emit("review_parsed", 1, review=reviewed)
        self._emit("spelling_started", 1)
        try:
            matches = self._spelling_matches(text)
        except AgentError as exc:
            self._emit("spelling_failed", 1, error=exc.code)
            raise
        self._emit("spelling_returned", 1, matches=matches)
        adjudicated, unresolved = rubric_spelling(source, text, matches, self.review_rubric,
            allow_spacetime=self.review_rubric == MEANING_FIRST_REVIEW_RUBRIC)
        self._emit("spelling_adjudicated", 1, adjudicated=adjudicated, unresolved=unresolved)
        passed = reviewed["approved"] and not reviewed["issues"]
        human_rejection = (self.allow_human_rejection and reviewed["approved"] is False
                           and bool(reviewed["issues"]))
        if not ((passed or human_rejection) and reviewed["topic"] in TOPICS and not unresolved):
            feedback = {"review_approved": reviewed["approved"], "topic": reviewed["topic"],
                        "review_issues": reviewed["issues"], "spelling_matches": unresolved[:20]}
            self._emit("rejected", 1, mode="candidate", error="ARTICLE_QUALITY_REJECTED", feedback=feedback)
            raise AgentError("ARTICLE_QUALITY_REJECTED")
        prepared = {"title": title, "body": full, "translation": text,
                    "original": source, "url": url, "topic": reviewed["topic"]}
        self._human_evidence(prepared, original_title, original, reviewed, raw_review, matches,
                             adjudicated, unresolved, fresh_spelling=True)
        self._emit("accepted" if passed else "rejected_for_human", 1, mode="candidate", result=prepared)
        return prepared

    def recover_review_protocol(self, title, body, original_title, original, url, *,
                                raw_review, expected_review_prompt_hash):
        """Revalidate one saved positive advisory overflow, never sample a model.

        The caller must bind the old trace, exact inputs, policy and one durable
        recovery claim. This method supplies fresh hard gates and LanguageTool;
        it neither stages a draft nor grants publication approval.
        """
        error = "ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"
        if (self.review_rubric != MEANING_FIRST_REVIEW_RUBRIC
                or expected_review_prompt_hash != self.review_prompt_hash):
            raise AgentError(error, 409)
        canonical_url = canonical_article_url(url)
        en_title, en_body, source = _validated_source(original_title, original)
        if (canonical_url != url or en_title != original_title or en_body != original
                or not isinstance(title, str) or not title.strip()
                or not isinstance(body, str) or not body.strip()
                or title != normalize_article_links(title.strip())
                or body != normalize_article_links(body.strip())):
            raise AgentError(error, 409)
        reviewed = protocol_recovery_review(raw_review)
        protocol = protocol_recovery_provenance(raw_review, self.review_prompt_hash)
        self._emit("protocol_recovery_started", 1, protocol=protocol, raw=raw_review,
                   candidate={"title": title, "body": body},
                   source_untrusted={"title": en_title, "body": en_body, "url": url},
                   **({"reference_contract": self.reference_contract}
                      if self.reference_contract == REFERENCE_CONTRACT else {}))
        self._emit("protocol_recovery_validated", 1, protocol=protocol, review=reviewed)
        text = title + "\n\n" + body
        full = text + "\n\n" + article_footer(url)
        try:
            rubric_translation_gate(source, text, self.review_rubric,
                                    reference_contract=self.reference_contract)
            if len(full) > 3500:
                raise AgentError("ARTICLE_POST_TOO_LONG")
        except AgentError as exc:
            self._emit("gate_rejected", 1, error=exc.code, characters=len(full))
            self._emit("rejected", 1, mode="protocol_recovery", error=exc.code)
            raise
        self._emit("gate_passed", 1, characters=len(full))
        self._emit("spelling_started", 1)
        try:
            matches = self._spelling_matches(text)
        except AgentError as exc:
            self._emit("spelling_failed", 1, error=exc.code)
            raise
        self._emit("spelling_returned", 1, matches=matches)
        adjudicated, unresolved = rubric_spelling(source, text, matches, self.review_rubric)
        self._emit("spelling_adjudicated", 1, adjudicated=adjudicated, unresolved=unresolved)
        if unresolved:
            self._emit("rejected", 1, mode="protocol_recovery", error="ARTICLE_QUALITY_REJECTED",
                       feedback={"review_approved": True, "topic": reviewed["topic"],
                                 "review_issues": [], "spelling_matches": unresolved[:20]})
            raise AgentError("ARTICLE_QUALITY_REJECTED")
        prepared = {"title": title, "body": full, "translation": text, "original": source,
                    "url": url, "topic": reviewed["topic"], "review_protocol_recovery": protocol}
        self._human_evidence(prepared, en_title, en_body, reviewed, raw_review, matches, adjudicated, unresolved)
        prepared["machine_review_evidence"]["protocol_recovery"] = copy.deepcopy(protocol)
        self._emit("accepted", 1, mode="protocol_recovery", result=prepared)
        return prepared

    def recover_scientific_spelling(self, title, body, original_title, original, url, *,
                                    raw_review, expected_review_prompt_hash):
        """Reuse an exactly bound positive review with fresh hard gates and LT.

        The caller must verify the full failed trace and claim this recovery
        once. This method neither samples a model nor stages/approves anything.
        """
        error = "ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"
        if (self.review_rubric != MEANING_FIRST_REVIEW_RUBRIC
                or expected_review_prompt_hash != self.review_prompt_hash):
            raise AgentError(error, 409)
        canonical_url = canonical_article_url(url)
        en_title, en_body, source = _validated_source(original_title, original)
        if (canonical_url != url or en_title != original_title or en_body != original
                or not isinstance(title, str) or not title.strip()
                or not isinstance(body, str) or not body.strip()
                or title != normalize_article_links(title.strip())
                or body != normalize_article_links(body.strip())
                or not re.search(r"(?<![\w-])" + MULTISCALE_PHRASE + r"(?![\w-])", body)
                or not re.search(r"(?<![A-Za-z])" + re.escape(MULTISCALE_SOURCE_TERM) + r"(?![A-Za-z])", source)):
            raise AgentError(error, 409)
        reviewed = scientific_spelling_review(raw_review)
        self._emit("saved_review_recovery_started", 1, kind="scientific_spelling", raw=raw_review,
                   candidate={"title": title, "body": body},
                   source_untrusted={"title": en_title, "body": en_body, "url": url},
                   review_rubric=self.review_rubric, review_prompt_hash=self.review_prompt_hash,
                   **({"reference_contract": self.reference_contract}
                      if self.reference_contract == REFERENCE_CONTRACT else {}))
        self._emit("saved_review_recovery_validated", 1, kind="scientific_spelling", review=reviewed)
        text = title + "\n\n" + body
        full = text + "\n\n" + article_footer(url)
        try:
            rubric_translation_gate(source, text, self.review_rubric,
                                    reference_contract=self.reference_contract)
            if len(full) > 3500:
                raise AgentError("ARTICLE_POST_TOO_LONG")
        except AgentError as exc:
            self._emit("gate_rejected", 1, error=exc.code, characters=len(full))
            self._emit("rejected", 1, mode="scientific_spelling_recovery", error=exc.code)
            raise
        self._emit("gate_passed", 1, characters=len(full))
        self._emit("spelling_started", 1)
        try:
            matches = self._spelling_matches(text)
        except AgentError as exc:
            self._emit("spelling_failed", 1, error=exc.code)
            raise
        self._emit("spelling_returned", 1, matches=matches)
        adjudicated, unresolved = rubric_spelling(source, text, matches, self.review_rubric)
        self._emit("spelling_adjudicated", 1, adjudicated=adjudicated, unresolved=unresolved)
        if unresolved:
            self._emit("rejected", 1, mode="scientific_spelling_recovery", error="ARTICLE_QUALITY_REJECTED",
                       feedback={"review_approved": True, "topic": reviewed["topic"],
                                 "review_issues": [], "spelling_matches": unresolved[:20]})
            raise AgentError("ARTICLE_QUALITY_REJECTED")
        prepared = {"title": title, "body": full, "translation": text, "original": source,
                    "url": url, "topic": reviewed["topic"]}
        self._human_evidence(prepared, en_title, en_body, reviewed, raw_review, matches, adjudicated, unresolved)
        self._emit("accepted", 1, mode="scientific_spelling_recovery", result=prepared)
        return prepared

    def recover_roni_spelling(self, title, body, original_title, original, url, *,
                              raw_review, expected_review_prompt_hash):
        """Check a caller-bound positive RONI review with fresh gates and LT only."""
        error = "ARTICLE_RONI_SPELLING_NOT_RECOVERABLE"
        if (self.review_rubric != MEANING_FIRST_REVIEW_RUBRIC
                or expected_review_prompt_hash != self.review_prompt_hash):
            raise AgentError(error, 409)
        canonical_url = canonical_article_url(url)
        en_title, en_body, source = _validated_source(original_title, original)
        if (canonical_url != url or url != "https://svs.gsfc.nasa.gov/5685/"
                or en_title != original_title or en_body != original
                or not isinstance(title, str) or not title.strip()
                or not isinstance(body, str) or not body.strip()
                or title != normalize_article_links(title.strip())
                or body != normalize_article_links(body.strip())
                or not _RONI_PHRASE.search(title + "\n\n" + body)
                or not re.search(r"(?<![\w-])" + re.escape(RONI_SOURCE_TERM) + r"(?![\w-])", source)):
            raise AgentError(error, 409)
        reviewed = scientific_spelling_review(raw_review)
        self._emit("saved_review_recovery_started", 1, kind="roni_spelling", raw=raw_review,
                   candidate={"title": title, "body": body},
                   source_untrusted={"title": en_title, "body": en_body, "url": url},
                   review_rubric=self.review_rubric, review_prompt_hash=self.review_prompt_hash,
                   **({"reference_contract": self.reference_contract}
                      if self.reference_contract == REFERENCE_CONTRACT else {}))
        self._emit("saved_review_recovery_validated", 1, kind="roni_spelling", review=reviewed)
        text = title + "\n\n" + body
        full = text + "\n\n" + article_footer(url)
        try:
            rubric_translation_gate(source, text, self.review_rubric,
                                    reference_contract=self.reference_contract)
            if len(full) > 3500:
                raise AgentError("ARTICLE_POST_TOO_LONG")
        except AgentError as exc:
            self._emit("gate_rejected", 1, error=exc.code, characters=len(full))
            self._emit("rejected", 1, mode="roni_spelling_recovery", error=exc.code)
            raise
        self._emit("gate_passed", 1, characters=len(full))
        self._emit("spelling_started", 1)
        try:
            matches = self._spelling_matches(text)
        except AgentError as exc:
            self._emit("spelling_failed", 1, error=exc.code)
            raise
        self._emit("spelling_returned", 1, matches=matches)
        adjudicated, unresolved, seen = [], [], set()
        for match in matches:
            evidence = roni_spelling_evidence(source, text, match)
            position = (match.get("offset"), match.get("length"))
            if evidence and len(matches) <= 2 and position not in seen:
                adjudicated.append(evidence)
                seen.add(position)
            else:
                unresolved.append(match)
        self._emit("spelling_adjudicated", 1, adjudicated=adjudicated, unresolved=unresolved)
        if unresolved:
            self._emit("rejected", 1, mode="roni_spelling_recovery", error="ARTICLE_QUALITY_REJECTED",
                       feedback={"review_approved": True, "topic": reviewed["topic"],
                                 "review_issues": [], "spelling_matches": unresolved[:20]})
            raise AgentError("ARTICLE_QUALITY_REJECTED")
        prepared = {"title": title, "body": full, "translation": text, "original": source,
                    "url": url, "topic": reviewed["topic"]}
        self._human_evidence(prepared, en_title, en_body, reviewed, raw_review, matches, adjudicated, unresolved)
        self._emit("accepted", 1, mode="roni_spelling_recovery", result=prepared)
        return prepared

    def close(self):
        self.spelling.close()
