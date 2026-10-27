"""Один ограниченный редактор кандидата; не проверка качества и не согласование."""

import copy
import hashlib
import json
import math
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from .article_attribution import article_footer
from .article_links import LEGACY_REFERENCE_CONTRACT, reference_urls
from .article_quality import (
    MEANING_FIRST_REVIEW_RUBRIC,
    _validated_source,
    canonical_article_url,
    normalize_article_links,
    rubric_translation_gate,
)
from .config import AgentError, fingerprint, parse_json
from .editorial import numbers

REPAIR_CONTRACT = "article-repair-v7"
LEGACY_REPAIR_CONTRACT = "article-repair-v3"
_LINK_CANONICAL_CONTRACTS = frozenset({"article-repair-v4", "article-repair-v5", "article-repair-v6", "article-repair-v7"})
REPAIR_CONTRACTS = frozenset({LEGACY_REPAIR_CONTRACT, *_LINK_CANONICAL_CONTRACTS})
REPAIR_MODEL = "proxmox/qwen35-9b"
MAX_FEEDBACK_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_CHANGES = 8


def repair_replay_hash(candidate):
    """Хеш запрета повтора; точные content/quality hashes он не заменяет."""
    return fingerprint({key: " ".join(normalize_article_links(candidate[key].strip()).split())
                        for key in ("title", "body")})


def _require(condition, code="ARTICLE_REPAIR_INPUT_INVALID"):
    if not condition:
        raise AgentError(code, 409)


def repair_prompt_name(contract):
    """Старые claims используют прежний неизменённый файл промпта."""
    _require(isinstance(contract, str) and contract in REPAIR_CONTRACTS,
             "ARTICLE_REPAIR_CONTRACT_INVALID")
    return {"article-repair-v5": "ARTICLE_REPAIR_V5.txt",
            "article-repair-v6": "ARTICLE_REPAIR_V6.txt",
            "article-repair-v7": "ARTICLE_REPAIR_V7.txt"}.get(contract, "ARTICLE_REPAIR.txt")


def repair_reference_contract(contract):
    """Новый extractor не меняет воспроизведение прежних трёх протоколов."""
    repair_prompt_name(contract)
    return "article-links-v2" if contract in {"article-repair-v6", "article-repair-v7"} else LEGACY_REFERENCE_CONTRACT


def repair_context(article, candidate, feedback, *, contract=REPAIR_CONTRACT):
    """Чистый контекст для нового запроса и точного replay сохранённого trace."""
    repair_prompt_name(contract)
    title, body, source = _validated_source(article["title"], article["original"])
    context = {"source_untrusted": {"title": title, "body": body,
                                    "url": canonical_article_url(article["url"])},
               "candidate_untrusted": copy.deepcopy(candidate),
               "feedback_untrusted": copy.deepcopy(feedback),
               "numeric_tokens_must_match_exactly": sorted(numbers(source))}
    if contract in {"article-repair-v5", "article-repair-v6", "article-repair-v7"}:
        # Диагностика использует тот же literal parser, что и hard gate.
        # Список помогает редактору; код не дописывает ссылки в его ответ.
        references = repair_reference_contract(contract)
        required = reference_urls(source, contract=references)
        current = reference_urls(candidate["title"] + "\n\n" + candidate["body"], contract=references)
        context["reference_url_manifest"] = {
            "contract": references, "required_urls": sorted(required),
            "current_urls": sorted(current), "missing_urls": sorted(required - current),
            "unexpected_urls": sorted(current - required),
        }
    return context


def _response_object(raw):
    """Разрешена одна внешняя JSON-ограда, но не поиск объекта среди прозы."""
    text = raw.strip()
    wrapper = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*)\r?\n```", text, flags=re.DOTALL)
    return parse_json(wrapper.group(1) if wrapper else text)


def _text_fields(value, *, title_limit, body_limit, code):
    _require(isinstance(value, dict) and set(value) == {"title", "body"}, code)
    _require(all(isinstance(item, str) and item.strip() for item in value.values()), code)
    _require(len(value["title"]) <= title_limit and len(value["body"]) <= body_limit, code)
    _require("Источник:" not in value["body"]
             and "Перевод и редактура с помощью нейросетей." not in value["body"], code)


def _latin_mask(before, after, *, guard_mixed=False):
    """Смена алфавита неизвестного слова не считается исправлением словаря."""
    old, new = (re.findall(r"[^\W\d_]+", value) for value in (before, after))
    for operation, left, right, start, end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if (operation == "replace" and 1 <= right - left <= 4 and 1 <= end - start <= 4
                and all(re.fullmatch(r"[А-Яа-яЁё]+", token) for token in old[left:right])
                and (any(any(unicodedata.name(char, "").startswith("LATIN ") for char in token)
                         for token in new[start:end]) if guard_mixed else
                     all(all(unicodedata.name(char, "").startswith("LATIN ") for char in token)
                         for token in new[start:end]))):
            return True
    return False


def _json_data(value, limit):
    """Данные остаются полными; не обрезаем raw замечания и не выполняем их."""
    try:
        pending = [(value, 0)]
        nodes = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            _require(depth <= 32 and nodes <= 20000)
            if type(item) is dict:
                _require(all(type(key) is str for key in item))
                pending.extend((part, depth + 1) for part in item.values())
            elif type(item) is list:
                pending.extend((part, depth + 1) for part in item)
            else:
                _require(type(item) in {str, int, float, bool, type(None)})
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        _require(len(encoded.encode("utf-8")) <= limit)
        # Вызывающая сторона передаёт обычные JSON-данные, без дополнительных типов.
        return parse_json(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AgentError("ARTICLE_REPAIR_INPUT_INVALID", 409) from exc


def _actual_diff(previous, candidate, *, contract=REPAIR_CONTRACT):
    """Полный diff вычисляется кодом; self-report модели не задаёт его полноту."""
    actual, mappings = [], {}
    for field in ("title", "body"):
        old, new = previous[field], candidate[field]
        mappings[field] = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        parts, cursor = [], 0
        for operation, left, right, start, end in mappings[field]:
            if operation == "equal":
                continue
            actual.append({"field": field, "old_start": left, "old_end": right,
                           "new_start": start, "new_end": end,
                           "before": old[left:right], "after": new[start:end]})
            parts.extend((old[cursor:left], new[start:end]))
            cursor = right
        parts.append(old[cursor:])
        _require("".join(parts) == new, "ARTICLE_REPAIR_PATCH_INVALID")
        _require(not _latin_mask(old, new, guard_mixed=contract == "article-repair-v7"),
                 "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN")
    return actual, mappings


def _mapped_boundaries(operations, position):
    """Вставка на границе даёт два допустимых края, без угадывания внутри замены."""
    points = set()
    for operation, left, right, start, end in operations:
        if operation == "equal" and left <= position <= right:
            points.add(start + position - left)
        if left == position:
            points.add(start)
        if right == position:
            points.add(end)
    return points


def _canonical_link_field(value):
    """Канонизируем лишь плоские HTTP(S)-ссылки с точными подписью и href.

    Общий normalizer переносит URL в конец. До него отсеиваем вложенные,
    опасные и незавершённые обёртки, которые он не должен молча обходить.
    Обычные пояснения в квадратных скобках остаются обычным текстом.
    """
    code = "ARTICLE_REPAIR_PATCH_INVALID"
    _require(not re.search(r"(?<!\\)!\[", value), code)
    opened, index = [], 0
    while index < len(value):
        char = value[index]
        if char == "[":
            opened.append(index)
        elif char == "]":
            start = opened.pop() if opened else None
            if value[index + 1:index + 2] == "(":
                _require(start is not None and not opened
                         and (start == 0 or value[start - 1] not in "!\\")
                         and value[start + 1:index].strip()
                         and not any(c in value[start + 1:index] for c in "\r\n"), code)
                end, depth = index + 2, 0
                _require(value[end:].startswith(("https://", "http://")), code)
                while end < len(value):
                    current = value[end]
                    _require(not current.isspace() and current not in '<>"[]\\', code)
                    if current == "(":
                        depth += 1
                    elif current == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    end += 1
                _require(end < len(value), code)
                try:
                    destination = urlsplit(value[index + 2:end])
                    _require(destination.scheme in {"http", "https"} and destination.hostname
                             and not destination.username and not destination.password, code)
                    # Обращение проверяет синтаксис порта, но не меняет literal URL.
                    _ = destination.port
                except ValueError as exc:
                    raise AgentError(code, 409) from exc
                index = end
        index += 1
    canonical = normalize_article_links(value.strip())
    _require(not re.search(r"\]\s*\(", canonical), code)
    return canonical


def _patch_candidate(previous, output, source, *, contract=REPAIR_CONTRACT):
    """Проверить заявленные замены и отдельно собрать полный фактический diff."""
    code = "ARTICLE_REPAIR_PATCH_INVALID"
    _require(isinstance(output, dict), code)
    if set(output) == {"title", "body"}:
        # Основной плоский ответ не требует от модели повторного описания diff.
        candidate, changes = output, []
        if contract in _LINK_CANONICAL_CONTRACTS:
            # Raw уже ограничен 128 KiB. Размер поста проверяется после
            # канонизации; сами Markdown-обёртки не являются текстом поста.
            _text_fields(candidate, title_limit=MAX_RESPONSE_BYTES,
                         body_limit=MAX_RESPONSE_BYTES, code=code)
            candidate = {key: _canonical_link_field(value) for key, value in candidate.items()}
    else:
        # Сохранённые корректные nested ответы остаются читаемыми для аудита.
        _require(set(output) == {"candidate", "changes"}, code)
        candidate, changes = output["candidate"], output["changes"]
    _text_fields(candidate, title_limit=500, body_limit=3500, code=code)
    _require(all(value == normalize_article_links(value.strip()) for value in candidate.values()), code)
    _require(isinstance(changes, list) and len(changes) <= MAX_CHANGES, code)
    actual, mappings = _actual_diff(previous, candidate, contract=contract)
    spans = {"title": [], "body": []}
    for change in changes:
        _require(isinstance(change, dict)
                 and set(change) == {"field", "before", "after", "source_quote", "reason"}, code)
        field = change["field"]
        _require(isinstance(field, str) and field in spans, code)
        before, after, quote, reason = (change[key] for key in ("before", "after", "source_quote", "reason"))
        _require(all(isinstance(item, str) for item in (before, after, quote, reason)), code)
        _require(before.strip() and after.strip() and before != after
                 and len(before) <= 16000 and len(after) <= 3500
                 and 3 <= len(quote.strip()) <= 1200 and quote in source
                 and 10 <= len(reason.strip()) <= 1000, code)
        # Консервативно отклоняем даже потенциально верный original-name вариант:
        # его нельзя использовать как автоматическое подавление словаря LT.
        _require(not _latin_mask(before, after, guard_mixed=contract == "article-repair-v7"),
                 "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN")
        _require(previous[field].count(before) == 1, code)
        start = previous[field].index(before)
        end = start + len(before)
        lefts = _mapped_boundaries(mappings[field], start)
        rights = _mapped_boundaries(mappings[field], end)
        _require(any(left + len(after) in rights and candidate[field][left:left + len(after)] == after
                     for left in lefts), code)
        spans[field].append((start, start + len(before), after))
    for replacements in spans.values():
        cursor = 0
        for start, end, _after in sorted(replacements):
            _require(start >= cursor, code)
            cursor = end
    # Цитата и reason — аудируемая привязка, а не доказательство верности смысла.
    # Независимые Ministral и LanguageTool обязательны после этого генератора.
    return candidate, actual, changes


class ArticleRepair:
    """Вызывающая сторона заранее фиксирует durable claim, источник и известный отказ."""

    def __init__(self, editor, *, prompt_dir=None, trace=None, contract=REPAIR_CONTRACT):
        _require(getattr(editor, "model", None) == REPAIR_MODEL, "ARTICLE_REPAIR_MODEL_REQUIRED")
        _require(trace is None or callable(trace), "ARTICLE_REPAIR_TRACE_INVALID")
        _require(isinstance(contract, str) and contract in REPAIR_CONTRACTS,
                 "ARTICLE_REPAIR_CONTRACT_INVALID")
        self.editor, self.trace = editor, trace
        self.contract = contract
        prompts = Path(prompt_dir) if prompt_dir else Path(__file__).resolve().parents[2] / "prompts"
        self.instruction = (prompts / repair_prompt_name(contract)).read_text(encoding="utf-8")
        self.prompt_hash = hashlib.sha256(self.instruction.encode("utf-8")).hexdigest()

    def _emit(self, phase, **details):
        if self.trace is not None:
            try:
                self.trace(copy.deepcopy({"phase": phase, "attempt": 1, **details}))
            except Exception as exc:
                raise AgentError("ARTICLE_REPAIR_TRACE_FAILED", 503) from exc

    def propose(self, article, candidate, feedback, *, rejected_replay_hashes=()):
        """Создать один изменённый кандидат либо явный безопасный отказ без новой оценки."""
        _require(isinstance(article, dict) and set(article) == {"url", "title", "original", "published_at"})
        _require(type(article["published_at"]) in {int, float}
                 and math.isfinite(article["published_at"]) and article["published_at"] >= 0)
        url = canonical_article_url(article["url"])
        _require(url == article["url"])
        _en_title, _en_body, source = _validated_source(article["title"], article["original"])
        _text_fields(candidate, title_limit=1000, body_limit=16000, code="ARTICLE_REPAIR_INPUT_INVALID")
        _require(isinstance(feedback, dict) and bool(feedback))
        _require(isinstance(rejected_replay_hashes, (list, tuple, set, frozenset))
                 and len(rejected_replay_hashes) <= 32)
        _require(all(isinstance(value, str) and len(value) == 64
                     and all(char in "0123456789abcdef" for char in value) for value in rejected_replay_hashes))
        article = _json_data(article, 40000)
        previous = _json_data(candidate, 70000)
        diagnostic = _json_data(feedback, MAX_FEEDBACK_BYTES)
        _require(isinstance(self.contract, str) and self.contract in REPAIR_CONTRACTS,
                 "ARTICLE_REPAIR_CONTRACT_INVALID")
        binding = {"contract": self.contract, "model": REPAIR_MODEL, "prompt_hash": self.prompt_hash,
                   "input_hash": fingerprint(article), "old_candidate_hash": fingerprint(previous),
                   "old_replay_hash": repair_replay_hash(previous), "feedback_hash": fingerprint(diagnostic)}
        if self.contract in {"article-repair-v6", "article-repair-v7"}:
            binding["reference_contract"] = repair_reference_contract(self.contract)
        context = repair_context(article, previous, diagnostic, contract=self.contract)
        prompt = self.instruction + "\n\nUNTRUSTED_JSON_DATA:\n" + json.dumps(context, ensure_ascii=False)
        self._emit("repair_started", binding=binding, context=context)
        try:
            # Не используем OpenCode.run: в нём есть дополнительные format repairs.
            # Здесь на durable claim приходится ровно один запрос редактору.
            response = self.editor.generate_text(prompt)
            _require(isinstance(response, tuple) and len(response) == 2, "ARTICLE_REPAIR_RESPONSE_INVALID")
            raw = response[0]
            _require(isinstance(raw, str), "ARTICLE_REPAIR_RESPONSE_INVALID")
            # Сохраняем и невалидный полный ответ до его разбора и проверки размера.
            self._emit("repair_returned", raw=raw, binding=binding)
            _require(raw.strip() and len(raw.encode("utf-8")) <= MAX_RESPONSE_BYTES,
                     "ARTICLE_REPAIR_RESPONSE_INVALID")
            evidence = {**binding, "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
            try:
                output = _response_object(raw)
            except AgentError as exc:
                raise AgentError("ARTICLE_REPAIR_RESPONSE_INVALID", 409) from exc
            if isinstance(output, dict) and set(output) == {"repairable", "reason"}:
                _require(output["repairable"] is False and isinstance(output["reason"], str)
                         and 10 <= len(output["reason"].strip()) <= 2000, "ARTICLE_REPAIR_RESPONSE_INVALID")
                evidence["outcome"] = "not_repairable"
                result = {**output, "evidence": evidence, "raw": raw}
                self._emit("repair_not_repairable", result=result)
                return result
            proposed, actual, reported = _patch_candidate(previous, output, source, contract=self.contract)
            replay = repair_replay_hash(proposed)
            if replay == binding["old_replay_hash"] or replay in rejected_replay_hashes:
                raise AgentError("ARTICLE_REPAIR_UNCHANGED", 409)
            text = proposed["title"] + "\n\n" + proposed["body"]
            rubric_translation_gate(source, text, MEANING_FIRST_REVIEW_RUBRIC,
                                    reference_contract=repair_reference_contract(self.contract))
            _require(len(text + "\n\n" + article_footer(url)) <= 3500,
                     "ARTICLE_POST_TOO_LONG")
            evidence.update(outcome="candidate", new_candidate_hash=fingerprint(proposed),
                            replay_hash=replay, actual_changes_hash=fingerprint(actual),
                            model_changes_hash=fingerprint(reported))
            if self.contract in _LINK_CANONICAL_CONTRACTS and set(output) == {"title", "body"}:
                # Исторический v3 не получает новых полей. Полные raw и
                # исходный кандидат v4/v5 позволяют воспроизвести преобразование.
                evidence["link_normalization"] = {
                    "contract": "article-links-v1", "raw_candidate": copy.deepcopy(output),
                    "raw_candidate_hash": fingerprint(output),
                    "canonical_candidate_hash": fingerprint(proposed),
                }
            result = {"candidate": proposed, "actual_changes": actual,
                      "model_changes": reported, "evidence": evidence, "raw": raw}
            self._emit("repair_candidate", result=result)
            return result
        except AgentError as exc:
            if exc.code != "ARTICLE_REPAIR_TRACE_FAILED":
                self._emit("repair_failed", error=exc.code, binding=binding)
            raise
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            self._emit("repair_failed", error="ARTICLE_REPAIR_RESPONSE_INVALID", binding=binding)
            raise AgentError("ARTICLE_REPAIR_RESPONSE_INVALID", 409) from exc
