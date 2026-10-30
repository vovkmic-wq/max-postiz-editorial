"""Версионированное чтение literal ссылок; без загрузки и исправления URL."""

import re

from .config import AgentError

LEGACY_REFERENCE_CONTRACT = "article-links-v1"
REFERENCE_CONTRACT = "article-links-v2"
REFERENCE_CONTRACTS = frozenset({LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT})

_URL_START = re.compile(r"https?://", re.IGNORECASE)


def reference_urls(text, *, contract):
    """Вернуть точные URL для диагностики и последующего сравнения множеств.

    V1 воспроизводит исторический regex. V2 сохраняет внутренние скобки,
    percent encoding, query и fragment. Повреждённый кандидат не чинится:
    его исходный токен остаётся в результате для missing/unexpected manifest.
    Это не проверка безопасности href: её выполняет сборщик источника.
    """
    if not isinstance(contract, str) or contract not in REFERENCE_CONTRACTS:
        raise AgentError("ARTICLE_REFERENCE_CONTRACT_INVALID", 409)
    if contract == LEGACY_REFERENCE_CONTRACT:
        return {value.rstrip(".,;") for value in re.findall(r"https?://[^\s<>\"\]\)]+", text)}

    # В Markdown [подпись](URL) и прозе (URL) перед URL есть внешняя «(».
    # Только парная ей «)» вне внутренних скобок URL завершает обёртку.
    # Без такой границы повреждённый токен сохраняется полностью.
    result, cursor = set(), 0
    for match in _URL_START.finditer(text):
        start = match.start()
        if start < cursor:
            continue
        wrapped = start > 0 and text[start - 1] == "("
        depth, end = 0, start
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
        # У bare URL неоднозначная завершающая пунктуация является частью
        # токена. Её нельзя молча отбросить из query, fragment или пути.
        result.add(text[start:end])
        cursor = end + 1 if end < len(text) and text[end] == ")" and wrapped else end
    return result
