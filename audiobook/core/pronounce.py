"""Словарь произношений книги: как читать имена и термины.

TTS часто ломает выдуманные имена. Здесь они заменяются на фонетическую
запись перед синтезом, а в тексте главы остаются как есть — читатель видит
исходное написание, слышит правильное.

Замена попадает в текст, от которого считается отпечаток реплики, поэтому
правка словаря переозвучивает ровно те реплики, где это слово встречается.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

__all__ = ["compile_rules", "apply", "affected"]

Rule = dict


def _pattern(rule: Rule) -> re.Pattern[str] | None:
    term = (rule.get("term") or "").strip()
    if not term:
        return None
    body = re.escape(term)
    if rule.get("whole_word", True):
        # Границы по «не-букве»: \b в кириллице ведёт себя так же, но с
        # дефисными именами вроде «Ре-Даль» \w-класс надёжнее.
        body = rf"(?<!\w){body}(?!\w)"
    flags = 0 if rule.get("case_sensitive") else re.IGNORECASE
    return re.compile(body, flags)


def compile_rules(rules: Iterable[Rule]) -> list[tuple[re.Pattern[str], str]]:
    """Готовые правила, длинные термины — первыми.

    Иначе «Рудеус» внутри «Рудеус Грейрат» подменился бы раньше, чем целое имя.
    """
    prepared = []
    for rule in sorted(rules, key=lambda r: len(r.get("term") or ""), reverse=True):
        pattern = _pattern(rule)
        if pattern is not None:
            prepared.append((pattern, rule.get("replacement") or ""))
    return prepared


def apply(text: str, rules: Sequence[Rule] | Sequence[tuple[re.Pattern[str], str]]) -> str:
    """Применить словарь к тексту реплики."""
    if not text or not rules:
        return text or ""
    prepared = rules if rules and isinstance(rules[0], tuple) else compile_rules(rules)
    for pattern, replacement in prepared:
        # lambda, а не строка: в замене могут быть \1 и прочие спецпоследовательности.
        text = pattern.sub(lambda _m, value=replacement: value, text)
    return text


def affected(text: str, rules: Sequence[Rule]) -> list[str]:
    """Термины словаря, которые встречаются в тексте."""
    found = []
    for rule in rules:
        pattern = _pattern(rule)
        if pattern is not None and pattern.search(text or ""):
            found.append(rule.get("term", ""))
    return found
