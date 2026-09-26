"""Сведения о книге: жанры, настроение, метки — и подбор их через Claude.

Жанр и настроение выбираются из коротких списков: по ним рисуется обложка
(жанр — фигура, настроение — цвета), поэтому свободный текст там не нужен.
Метки — свободные, для поиска и для себя.

Подбор идёт в два шага. Сначала Claude получает только название и то, что
человек знает об авторе, годе и стране, и описывает произведение целиком —
как его знают читатели. Только если книгу он не узнал, второй запрос несёт
начало текста: по отрывку модель хотя бы поймёт, о чём книга. Раньше отрывок
шёл сразу, и метки описывали одну сцену, а не книгу. Ответ только
предлагается: сохраняет его человек.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from . import repo
from .markup import DEFAULT_MODEL, MarkupConfigError, _import_anthropic

log = logging.getLogger("audiobook.bookinfo")

__all__ = [
    "GENRES",
    "MOODS",
    "BookInfoError",
    "vocabulary",
    "suggest_info",
    "parse_suggestion",
]

Conn = sqlite3.Connection

# Только книжные жанры, как на полках магазина. Узкие поджанры (исекай,
# киберпанк, LitRPG) — это метки: жанров должно хватать, чтобы нарисовать обложку.
GENRES = (
    "Фэнтези", "Фантастика", "Детектив", "Триллер", "Ужасы", "Мистика",
    "Приключения", "Любовный роман", "Исторический", "Драма", "Юмор", "Сказка",
    "Классика", "Поэзия", "Детское", "Нон-фикшн",
)
MOODS = (
    "Светлое", "Весёлое", "Уютное", "Романтичное", "Спокойное", "Грустное",
    "Мрачное", "Напряжённое", "Загадочное", "Эпичное",
)

MAX_GENRES = 3
MAX_MOODS = 2
MAX_TAGS = 8
EXCERPT_CHARS = 2500
MAX_TOKENS = 1500
ATTEMPTS = 2

# Формат ответа — общий для обоих шагов.
ANSWER_FORMAT = f"""Верни ОДИН JSON-объект без пояснений вокруг:
{{
  "recognized": true,
  "title": "каноническое название на русском",
  "author": "автор оригинала",
  "year": 2012,
  "country": "страна автора по-русски",
  "genres": ["до {MAX_GENRES} жанров"],
  "moods": ["до {MAX_MOODS} настроений"],
  "tags": ["до {MAX_TAGS} коротких меток"],
  "description": "2–3 предложения о завязке, без спойлеров",
  "note": "в чём не уверен, или пустая строка"
}}

Правила для полей:
- genres — только из списка: {", ".join(GENRES)}.
- moods — только из списка: {", ".join(MOODS)}.
- tags — строчными буквами, 1–3 слова: узкие поджанры, темы, место действия,
  приёмы («исекай», «киберпанк», «перерождение», «школа магии»,
  «викторианский Лондон»). Не повторяй жанры.
- year — год первой публикации оригинала, целое число.
- Описание — без спойлеров и оценок, по-русски."""

# Шаг 1: узнать книгу по названию и описать её целиком.
SYSTEM_PROMPT = f"""Ты — библиотекарь, который заполняет карточку аудиокниги.
Тебе дают название произведения и иногда автора, год и страну. Узнай
произведение и опиши его ЦЕЛИКОМ — так, как его знают читатели: жанр,
настроение и главные темы всей книги (или всего цикла, если это том цикла),
а не отдельной сцены.

{ANSWER_FORMAT}
- Если человек указал автора, год или страну с ошибкой и ты уверен в этом —
  исправь и объясни в note.
- Не узнал произведение уверенно — поставь "recognized": false и оставь
  остальные поля пустыми. Ничего не выдумывай: тогда пришлют отрывок."""

# Шаг 2: книгу не узнали — описать по началу текста.
EXCERPT_PROMPT = f"""Ты — библиотекарь, который заполняет карточку аудиокниги.
Произведение по названию узнать не удалось, поэтому тебе дают начало текста.
Определи по нему жанр, настроение и темы книги и опиши завязку. Поставь
"recognized": false, а автора, год и страну оставь такими, как их дал
человек (или пустыми).

{ANSWER_FORMAT}"""


class BookInfoError(RuntimeError):
    """Сведения не подобрать: нет ключа, сервер отказал или ответ не разобрать."""


def vocabulary() -> dict[str, list[str]]:
    return {"genres": list(GENRES), "moods": list(MOODS)}


def _excerpt(conn: Conn, book_id: int, limit: int = EXCERPT_CHARS) -> str:
    parts: list[str] = []
    size = 0
    for chapter in repo.list_chapters(conn, book_id):
        if size >= limit:
            break
        piece = chapter.text[: limit - size]
        if piece.strip():
            parts.append(piece)
            size += len(piece)
    return "\n\n".join(parts)


def _user_message(title: str, author: str, year: Any, country: str, excerpt: str = "") -> str:
    known = [f"Название: {title}"]
    known.append(f"Автор: {author}" if author else "Автор: не указан")
    known.append(f"Год: {year}" if year else "Год: не указан")
    known.append(f"Страна: {country}" if country else "Страна: не указана")
    text = "\n".join(known)
    return text + f"\n\nНачало текста:\n<<<\n{excerpt}\n>>>" if excerpt else text


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _pick(values: Any, allowed: tuple[str, ...], limit: int) -> list[str]:
    """Оставить только значения из списка, сравнивая без регистра и «ё»."""
    fold = lambda text: str(text).casefold().replace("ё", "е").strip()  # noqa: E731
    known = {fold(item): item for item in allowed}
    out: list[str] = []
    for value in values if isinstance(values, list) else []:
        match = known.get(fold(value))
        if match and match not in out:
            out.append(match)
    return out[:limit]


def parse_suggestion(raw: str) -> dict[str, Any]:
    """Разобрать ответ модели в карточку: лишнее отбросить, непонятное обнулить."""
    text = _FENCE.sub("", (raw or "").strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise BookInfoError("модель ответила не карточкой книги — попробуйте ещё раз")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise BookInfoError(f"ответ модели не разобрать: {exc}") from exc
    if not isinstance(data, dict):
        raise BookInfoError("модель ответила не карточкой книги — попробуйте ещё раз")

    year = data.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not -3000 <= year <= 2100:
        year = None
    tags = [
        str(tag).strip().lower()
        for tag in (data.get("tags") if isinstance(data.get("tags"), list) else [])
        if str(tag).strip()
    ]
    genres = _pick(data.get("genres"), GENRES, MAX_GENRES)
    folded = {g.casefold() for g in genres}
    return {
        "recognized": bool(data.get("recognized")),
        "title": str(data.get("title") or "").strip()[:300],
        "author": str(data.get("author") or "").strip()[:300],
        "year": year,
        "country": str(data.get("country") or "").strip()[:60],
        "genres": genres,
        "moods": _pick(data.get("moods"), MOODS, MAX_MOODS),
        "tags": [t for t in repo.clean_labels(tags) if t.casefold() not in folded][:MAX_TAGS],
        "description": str(data.get("description") or "").strip()[:2000],
        "note": str(data.get("note") or "").strip()[:500],
    }


def _explain(exc: Exception, ah: Any) -> str:
    """Ошибка SDK человеческими словами — она уйдёт прямо в интерфейс."""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, ah.AuthenticationError) or status == 401:
        return "ключ Anthropic не подошёл — проверьте его в «Настройках»"
    if isinstance(exc, ah.PermissionDeniedError) or status == 403:
        return "сервер отказал в доступе (403) — проверьте адрес и ключ в «Настройках»"
    if isinstance(exc, ah.NotFoundError) or status == 404:
        return "сервер не знает такую модель или адрес — проверьте «Настройки»"
    if isinstance(exc, ah.RateLimitError) or status == 429:
        return "слишком много запросов (429) — подождите минуту и повторите"
    if isinstance(exc, (ah.APIConnectionError, ah.APITimeoutError)):
        return "нет связи с сервером Claude — проверьте интернет и адрес в «Настройках»"
    if status and status >= 500:
        return f"сервер Claude временно недоступен ({status}) — повторите позже"
    return f"не удалось подобрать сведения: {exc}"


def suggest_info(
    conn: Conn,
    book_id: int,
    *,
    title: str,
    author: str = "",
    year: Any = None,
    country: str = "",
    client: Any = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Спросить у Claude жанры, настроение, метки и описание книги."""
    from .library import markup_connection

    title = " ".join((title or "").split())
    if not title:
        raise BookInfoError("укажите название произведения — без него не угадать")
    repo.get_book(conn, book_id)
    connection = markup_connection(conn, base_url=base_url)
    try:
        ah = _import_anthropic()
    except MarkupConfigError as exc:
        raise BookInfoError(str(exc)) from exc
    if client is None:
        if not connection["api_key"] and not connection["base_url"]:
            raise BookInfoError("нет ключа Anthropic — добавьте его в «Настройках», раздел «Ключи»")
        options = {k: v for k, v in connection.items() if k in ("api_key", "base_url") and v}
        client = ah.Anthropic(**options)

    model = connection["model"] or DEFAULT_MODEL
    # Сначала — только по названию: узнанную книгу описываем целиком.
    known = _ask(conn, client, ah, model, SYSTEM_PROMPT,
                 _user_message(title, author.strip(), year, country.strip()), book_id)
    if known["recognized"]:
        return known
    # Не узнал — теперь с началом текста: хоть что-то о книге из неё самой.
    excerpt = _excerpt(conn, book_id)
    if not excerpt.strip():
        return known
    guessed = _ask(conn, client, ah, model, EXCERPT_PROMPT,
                   _user_message(title, author.strip(), year, country.strip(), excerpt), book_id)
    guessed["from_excerpt"] = True
    return guessed


def _ask(conn: Conn, client: Any, ah: Any, model: str, system: str, message: str, book_id: int) -> dict[str, Any]:
    """Один запрос к Claude с повтором при сбоях сети и лимитах."""
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": message}],
            )
            break
        except (ah.RateLimitError, ah.APIConnectionError, ah.APITimeoutError, ah.InternalServerError) as exc:
            last = exc
            log.warning("сведения о книге: попытка %d/%d не удалась: %s", attempt, ATTEMPTS, exc)
        except ah.APIError as exc:
            raise BookInfoError(_explain(exc, ah)) from exc
        except TypeError as exc:  # SDK без ключа падает так
            if "authentication" not in str(exc).lower():
                raise
            raise BookInfoError("нет ключа Anthropic — добавьте его в «Настройках», раздел «Ключи»") from exc
    else:
        raise BookInfoError(_explain(last, ah)) from last

    usage = getattr(response, "usage", None)
    repo.record_usage(
        conn, "anthropic", "book-info",
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        book_id=book_id,
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise BookInfoError("модель отказалась описывать эту книгу")
    raw = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    suggestion = parse_suggestion(raw)
    suggestion["model"] = getattr(response, "model", "") or model
    return suggestion
