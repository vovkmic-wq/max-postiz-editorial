"""Узкие source-bound доказательства трёх научных написаний для LanguageTool.

Это только лексическая сверка точного UnknownWord. Исходный match сохраняется;
грамматика, смысл, остальные слова и решение независимого редактора не меняются.
Снимки первичных источников закреплены SHA; сетевых запросов здесь нет.
"""
import copy
import re
import unicodedata

SCIENTIFIC_SPELLING_CONTRACT = "source-bound-terms-v1"
HUBBLE_EVIDENCE_URL = "https://iki.cosmos.ru/news/2026/07/28/kak-provodit-nablyudeniya-v-vodorodnom-oblake"
HUBBLE_REFERENCE_SHA256 = "b52a2b774255f1304c5be2a2b13649d48d489405bd5e82efb0bf3f92b36cbce9"
GODDARD_EVIDENCE_URL = "https://iki.cosmos.ru/research/missions/lend"
GODDARD_REFERENCE_SHA256 = "0f2d233f60ddb756e49c4c361e4cf03e53f5cb79fa724c09153d25063f4c362d"
STAR_FORMING_EVIDENCE_URL = "https://www.sao.ru/Doc-k8/Science/Public/Bulletin/Vol78/N3/ASPB78_03_347W.pdf"
STAR_FORMING_REFERENCE_SHA256 = "551ffd315296b57fbbab779a48aa6feb5d35f985efac812e76f96a4442236421"

HUBBLE_FORMS = frozenset({"Хаббл", "Хаббла", "Хабблу", "Хабблом", "Хаббле"})
STAR_FORMING_FORMS = frozenset({
    "звездообразующий", "звездообразующего", "звездообразующему", "звездообразующим",
    "звездообразующем", "звездообразующая", "звездообразующей", "звездообразующую",
    "звездообразующею", "звездообразующее", "звездообразующие", "звездообразующих",
    "звездообразующими",
    "Звездообразующий", "Звездообразующего", "Звездообразующему", "Звездообразующим",
    "Звездообразующем", "Звездообразующая", "Звездообразующей", "Звездообразующую",
    "Звездообразующею", "Звездообразующее", "Звездообразующие", "Звездообразующих",
    "Звездообразующими",
})
_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"']+")
_JOINED = re.compile(r"[\w-]", re.UNICODE)
_HUBBLE_SOURCE = re.compile(r"(?<![\w-])Hubble\s+Space\s+Telescope(?![\w-])", re.IGNORECASE)
_GODDARD_SOURCE = re.compile(r"(?<![\w-])Goddard\s+Space\s+Flight\s+Center(?![\w-])", re.IGNORECASE)
_STAR_SOURCE = re.compile(r"(?<![\w-])star-forming(?![\w-])", re.IGNORECASE)
_HUBBLE_LEFT = re.compile(
    r"(?<![\w-])телескоп(?:а|у|ом|е)?"
    r"(?:[ \t]+(?:NASA/ESA|НАСА/ЕКА|NASA|ESA|НАСА|ЕКА))?"
    r"(?:[ \t]+(?:им\.|имени))?[ \t]+[«„“\"']?$", re.IGNORECASE)
_HUBBLE_RIGHT = re.compile(
    r"^[»”\"']?[ \t]*(?:[—–-][ \t]*|,[ \t]+|\([ \t]*)"
    r"(?:космический[ \t]+)?телескоп(?:а|у|ом|е)?(?![\w-])", re.IGNORECASE)
_HUBBLE_WORD = re.compile(r"(?<![\w-])(?:Хабблом|Хаббла|Хабблу|Хаббле|Хаббл)(?![\w-])")
_GODDARD_LEFT = re.compile(
    r"(?<![\w-])центр(?:а|у|ом|е)?[ \t]+космических[ \t]+пол[её]тов"
    r"(?:[ \t]+(?:NASA|НАСА))?"
    r"(?:[ \t]+(?:им\.|имени))?[ \t]+[«„“\"']?$", re.IGNORECASE)


def _narrative(text):
    # Позиции Python сохраняются; URL path/query/fragment не являются исходной речью.
    return _URL.sub(lambda found: " " * len(found.group()), text)


def _source_term(pattern, text):
    # re.IGNORECASE также знает long-s и некоторые Unicode casefold-двойники.
    # Английский термин допускает только ASCII-буквы, обычный дефис и пробелы.
    return any(all(char.isascii() or char.isspace() for char in found.group())
               and not (found.start() and _joined(text[found.start() - 1]))
               and not (found.end() < len(text) and _joined(text[found.end()]))
               for found in pattern.finditer(text))


def _joined(char):
    return bool(_JOINED.fullmatch(char) or unicodedata.category(char) in {"Mn", "Mc", "Me", "Cf"}
                or char in {"‐", "‑"})


def _local_hubble_context(text, narrative, start, end):
    left = max(0, start - 100)
    before = _HUBBLE_LEFT.search(narrative[left:start])
    after = _HUBBLE_RIGHT.match(narrative[end:end + 70])
    if before is not None:
        first, last = left + before.start(), end
    elif after is not None:
        first, last = start, end + after.end()
    else:
        return None
    # Удалённый URL не должен искусственно соединять telescope и фамилию.
    if text[first:last] != narrative[first:last]:
        return None
    return {"phrase": text[first:last], "start": first, "end": last}


def _document_hubble_anchor(text, narrative):
    for found in _HUBBLE_WORD.finditer(narrative):
        if (found.start() and _joined(narrative[found.start() - 1])
                or found.end() < len(narrative) and _joined(narrative[found.end()])):
            continue
        anchor = _local_hubble_context(text, narrative, found.start(), found.end())
        if anchor is not None:
            return anchor
    return None


def _exact_word(translated, match):
    offset, length = match.get("offset"), match.get("length")
    if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
        return None
    try:
        encoded = translated.encode("utf-16-le")
        left, right = offset * 2, (offset + length) * 2
        if right > len(encoded):
            return None
        prefix = encoded[:left].decode("utf-16-le")
        word = encoded[left:right].decode("utf-16-le")
    except UnicodeError:
        return None
    start, end = len(prefix), len(prefix) + len(word)
    if (not word or translated[start:end] != word
            or start and _joined(translated[start - 1])
            or end < len(translated) and _joined(translated[end])):
        return None
    return word, start, end


def scoped_spelling_evidence(original, translated, match):
    """Вернуть доказательство только для одного точного LT spelling token, иначе None."""
    if not isinstance(original, str) or not isinstance(translated, str) or not isinstance(match, dict):
        return None
    if "\x00" in original or "\x00" in translated:
        return None
    rule, kind = match.get("rule"), match.get("type")
    if (not isinstance(rule, dict) or rule.get("id") != "MORFOLOGIK_RULE_RU_RU"
            or rule.get("issueType") != "misspelling" or not isinstance(rule.get("category"), dict)
            or rule["category"].get("id") != "TYPOS"
            or not isinstance(kind, dict) or kind.get("typeName") != "UnknownWord"):
        return None
    try:
        original.encode("utf-8")
    except UnicodeError:
        return None
    token = _exact_word(translated, match)
    if token is None:
        return None
    word, start, end = token
    source, candidate = _narrative(original), _narrative(translated)
    if candidate[start:end] != word:
        return None
    phrase = word
    context = {}
    if word in HUBBLE_FORMS and _source_term(_HUBBLE_SOURCE, source):
        local = _local_hubble_context(translated, candidate, start, end)
        if local is not None:
            phrase = local["phrase"]
        else:
            anchor = _document_hubble_anchor(translated, candidate)
            if anchor is None:
                return None
            context = {"context_scope": "document_telescope", "anchor_phrase": anchor["phrase"],
                "anchor_span": {"start": anchor["start"], "end": anchor["end"],
                                "offset_unit": "unicode_codepoint"}}
        term, url, hashed = "Hubble Space Telescope", HUBBLE_EVIDENCE_URL, HUBBLE_REFERENCE_SHA256
    elif word == "Годдарда" and _source_term(_GODDARD_SOURCE, source):
        before = _GODDARD_LEFT.search(candidate[max(0, start - 100):start])
        if before is None:
            return None
        phrase = before.group() + word
        term, url, hashed = "Goddard Space Flight Center", GODDARD_EVIDENCE_URL, GODDARD_REFERENCE_SHA256
    elif word in STAR_FORMING_FORMS and _source_term(_STAR_SOURCE, source):
        term, url, hashed = "star-forming", STAR_FORMING_EVIDENCE_URL, STAR_FORMING_REFERENCE_SHA256
    else:
        return None
    return {"match": copy.deepcopy(match), "word": word, "phrase": phrase, "source_term": term,
        "evidence_url": url, "reference_sha256": hashed, "contract": SCIENTIFIC_SPELLING_CONTRACT,
        "reason": "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM", **context}
