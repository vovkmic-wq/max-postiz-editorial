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

from .adapters import extract_object
from .collectors import detect_language
from .config import AgentError, fingerprint
from .editorial import numbers, translation_gate
from .network import resemblances_ip

SPELLING_URL = "http://127.0.0.1:8010/v2/check"
TOPICS = {"science", "stories", "humor"}
MAGELLANIC_EVIDENCE = "https://www.astronet.ru/db/msg/1167613/lmc.html"
MAX_REVIEW_ISSUES = 3
MAX_REVIEW_ISSUE_CHARS = 1024
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
    if (not isinstance(warnings, list) or len(warnings) > MAX_REVIEW_ISSUES
            or any(not isinstance(item, str) or not item.strip()
                   or len(item) > MAX_REVIEW_ISSUE_CHARS for item in warnings)):
        raise AgentError("ARTICLE_REVIEW_SCHEMA_INVALID")
    return {**normalized, "warnings": copy.deepcopy(warnings)}


def article_reference_urls(text):
    """Read literal reference URLs without dereferencing or rewriting them."""
    return {value.rstrip(".,;") for value in re.findall(r"https?://[^\s<>\"\]\)]+", text)}


def rubric_translation_gate(original, translated, rubric):
    """Keep numerical/language constraints and preserve every embedded reference."""
    if not isinstance(rubric, str) or rubric not in REVIEW_RUBRICS:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
    translation_gate(original, translated)
    if (rubric == MEANING_FIRST_REVIEW_RUBRIC
            and article_reference_urls(original) != article_reference_urls(translated)):
        raise AgentError("TRANSLATION_REFERENCE_CHANGED")


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


def adjudicate_scientific_spelling(original, translated, matches):
    """Resolve one documented scientific name; retain every other LT finding."""
    adjudicated, unresolved = [], []
    for match in matches:
        rule, kind = match.get("rule"), match.get("type")
        offset, length = match.get("offset"), match.get("length")
        evidence = None
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


def rubric_spelling(original, translated, matches, rubric):
    """Retain style suggestions as audited advice; grammar and unknown rules block."""
    if not isinstance(rubric, str) or rubric not in REVIEW_RUBRICS:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID")
    adjudicated, unresolved = adjudicate_scientific_spelling(original, translated, matches)
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
                 review_rubric=LEGACY_REVIEW_RUBRIC):
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
                        adjudicated, unresolved):
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
                    review_rubric=self.review_rubric, review_prompt_hash=self.review_prompt_hash)
        return prepared

    def prepare(self, title, original, url):
        url = canonical_article_url(url)
        title, original, source = _validated_source(title, original)
        context = {"instruction": self.translation_prompt,
                   "source_untrusted": {"title": title, "body": original, "url": url},
                   "numeric_tokens_must_match_exactly": sorted(numbers(source))}
        immutable_candidates = getattr(self.translator, "immutable_candidates", False) is True
        rejected_candidates = {}
        for attempt in range(1, 4):
            self._emit("translation_started", attempt, context=context)
            raw_translation = None
            try:
                raw_translation = self.translator.generate_text(
                    json.dumps(context, ensure_ascii=False))[0]
                self._emit("translation_returned", attempt, raw=raw_translation)
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
            full = text + "\n\nИсточник: " + url + "\nПеревод и редактура с помощью нейросетей."
            context["previous_translation"] = translated
            try:
                rubric_translation_gate(source, text, self.review_rubric)
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
            review_context = {"instruction": self.review_prompt,
                              "source_untrusted": source, "translation_untrusted": text}
            if self.review_rubric != LEGACY_REVIEW_RUBRIC:
                review_context.update(review_rubric=self.review_rubric,
                                      review_prompt_hash=self.review_prompt_hash)
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
            adjudicated, unresolved = rubric_spelling(source, text, matches, self.review_rubric)
            self._emit("spelling_adjudicated", attempt, adjudicated=adjudicated, unresolved=unresolved)
            passed = reviewed["approved"] and not reviewed["issues"]
            human_rejection = (self.allow_human_rejection and reviewed["approved"] is False
                               and bool(reviewed["issues"]))
            if ((passed or human_rejection) and reviewed["topic"] in TOPICS and not unresolved):
                prepared = {"title": translated["title"], "body": full, "translation": text,
                            "original": source, "url": url, "topic": reviewed["topic"]}
                self._human_evidence(prepared, title, original, reviewed, raw_review, matches,
                                     adjudicated, unresolved)
                self._emit("accepted" if passed else "rejected_for_human", attempt, result=prepared)
                return prepared
            context["repair_feedback"] = {"review_approved": reviewed["approved"],
                "topic": reviewed["topic"], "review_issues": reviewed["issues"],
                "spelling_matches": unresolved[:20]}
            rejected_candidates[text] = copy.deepcopy(context["repair_feedback"])
            self._emit("retry", attempt, feedback=context["repair_feedback"])
        self._emit("rejected", 3, error="ARTICLE_QUALITY_REJECTED",
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
        full = text + "\n\nИсточник: " + url + "\nПеревод и редактура с помощью нейросетей."
        self._emit("candidate_started", 1, mode="candidate", candidate={"title": title, "body": body},
                   source_untrusted={"title": original_title, "body": original, "url": url},
                   **({"candidate_raw": candidate_raw}
                      if candidate_raw != {"title": title, "body": body} else {}))
        try:
            rubric_translation_gate(source, text, self.review_rubric)
            if len(full) > 3500:
                raise AgentError("ARTICLE_POST_TOO_LONG")
        except AgentError as exc:
            self._emit("gate_rejected", 1, error=exc.code, characters=len(full))
            self._emit("rejected", 1, mode="candidate", error=exc.code)
            raise
        self._emit("gate_passed", 1, characters=len(full))
        review_context = {"instruction": self.review_prompt,
                          "source_untrusted": source, "translation_untrusted": text}
        if self.review_rubric != LEGACY_REVIEW_RUBRIC:
            review_context.update(review_rubric=self.review_rubric,
                                  review_prompt_hash=self.review_prompt_hash)
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
        adjudicated, unresolved = rubric_spelling(source, text, matches, self.review_rubric)
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
                             adjudicated, unresolved)
        self._emit("accepted" if passed else "rejected_for_human", 1, mode="candidate", result=prepared)
        return prepared

    def close(self):
        self.spelling.close()
