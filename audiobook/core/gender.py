"""Пол персонажа — по тексту книги, без обращения к модели.

В русском тексте пол почти всегда виден из глагола прошедшего времени рядом с
именем: «сказал Терех», «Аглая усмехнулась», «подумал Велимир». Такие глаголы
собираются по всей книге, и решает большинство: одно случайное существительное
на «-л» рядом с именем («стол», «угол») не перевешивает десяток «сказал».

Если глаголов не нашлось, решает окончание имени — с исключениями вроде
Никиты и Ильи. Если и оно молчит, пол остаётся неизвестным: лучше честное
«не знаю», чем уверенная ошибка.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

__all__ = ["MALE", "FEMALE", "guess_gender", "evidence"]

MALE = "м"
FEMALE = "ж"

WINDOW = 2  # сколько слов до и после имени смотреть
MIN_VOTES = 1

_WORD = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)?")
# Окно не должно перескакивать через границу фразы: в «…и пришла. Велимир
# перевёл» глагол «пришла» относится к Аглае, а не к Велимиру. Тире реплик
# тоже граница: «— Поздно, — сказал Терех» режется так, что глагол остаётся с именем.
_CLAUSE = re.compile(r"[.!?…;:—–«»\"\n]+")
# Прошедшее время: гласная + «л»/«лся» — мужской род, + «ла»/«лась» — женский.
_MASCULINE = re.compile(r"[аеёиоуыэюя]л(?:ся)?$")
_FEMININE = re.compile(r"[аеёиоуыэюя]ла(?:сь)?$")
_MIN_VERB = 4  # короче — скорее предлог или местоимение, чем глагол

# Мужские имена на «-а/-я»: по окончанию их легко принять за женские.
_MALE_ON_A = {
    "никита", "илья", "фома", "кузьма", "лука", "савва", "гаврила", "данила",
    "добрыня", "миша", "саша", "паша", "гоша", "лёша", "леша", "дима", "вова",
    "петя", "ваня", "коля", "толя", "федя", "серёжа", "сережа", "гриша", "яша",
    "женя", "слава", "юра", "костя", "стёпа", "степа", "митя", "витя", "боря",
    "сила", "мирча", "папа", "дядя", "дедушка", "староста", "судья", "слуга",
}
# Женские на согласную и мягкий знак, которые окончание бы не выдало.
_FEMALE_OTHER = {"любовь", "мать", "дочь", "нинель", "эсфирь", "руфь", "юдифь", "адель", "мишель"}
_MALE_WORDS = {"дед", "старик", "отец", "брат", "сын", "князь", "король", "царь", "монах", "стражник"}
_FEMALE_WORDS = {"бабка", "старуха", "мать", "сестра", "дочь", "княгиня", "королева", "царица", "монахиня"}


def _votes(name: str, texts: Iterable[str]) -> Counter:
    """Мужские и женские глаголы в окрестности имени по всем текстам."""
    stem = name.strip().lower()
    if not stem:
        return Counter()
    # Имя склоняется («Аглаю», «Тереха») — сравниваем по основе без окончания,
    # но глаголы берём только рядом с именительным падежом: он и есть подлежащее.
    votes: Counter = Counter()
    for text in texts:
        for clause in _CLAUSE.split(text or ""):
            words = [w.lower() for w in _WORD.findall(clause)]
            for index, word in enumerate(words):
                if word != stem:
                    continue
                around = words[max(0, index - WINDOW) : index] + words[index + 1 : index + 1 + WINDOW]
                for other in around:
                    if len(other) < _MIN_VERB:
                        continue
                    if _FEMININE.search(other):
                        votes[FEMALE] += 1
                    elif _MASCULINE.search(other):
                        votes[MALE] += 1
    return votes


def _by_ending(name: str) -> str:
    words = name.strip().lower().split()
    if not words:
        return ""
    first = words[0]
    # «Дед Терех», «Бабка Марфа» — род задаёт само слово.
    if first in _MALE_WORDS:
        return MALE
    if first in _FEMALE_WORDS:
        return FEMALE
    last = words[-1]
    if last in _MALE_ON_A or first in _MALE_ON_A:
        return MALE
    if last in _FEMALE_OTHER:
        return FEMALE
    if last.endswith(("а", "я")):
        return FEMALE
    if re.search(r"[бвгджзклмнпрстфхцчшщй]$", last):
        return MALE
    return ""  # «-ь», «-о», «-и» — не угадать


def evidence(name: str, texts: Iterable[str]) -> dict:
    """Пол и на чём он основан — для подсказки в интерфейсе."""
    votes = _votes(name, texts)
    male, female = votes[MALE], votes[FEMALE]
    if max(male, female) >= MIN_VOTES and male != female:
        return {"gender": MALE if male > female else FEMALE, "source": "text",
                "male": male, "female": female}
    ending = _by_ending(name)
    return {"gender": ending, "source": "ending" if ending else "", "male": male, "female": female}


def guess_gender(name: str, texts: Iterable[str]) -> str:
    """«м», «ж» или пусто, если в тексте и в имени подсказок нет."""
    return evidence(name, list(texts))["gender"]
