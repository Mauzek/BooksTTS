"""Автоматическая разметка главы по ролям через Claude.

Главу режем на батчи ~3000 символов с перекрытием в один абзац и передаём в
каждый батч список уже найденных персонажей — чтобы «Рудеус», «Рудэус» и
«Руди» не стали тремя разными голосами. Ответ — строго JSON-массив.

Идемпотентность: сегменты с ``is_manual`` повторная разметка не трогает.
Абзац, в котором есть хоть одна ручная правка, целиком остаётся как есть и в
модель не отправляется — так правки переживают перезапуск разметки, а заодно
экономятся запросы.
"""

from __future__ import annotations

import difflib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .models import EMOTIONS, NARRATOR, Segment
from .parser import Paragraph, paragraph_spans

__all__ = [
    "MarkupError",
    "MarkupConfigError",
    "BatchFailed",
    "MarkupParseError",
    "CharacterRegistry",
    "MarkupResult",
    "Issue",
    "markup_text",
    "build_batches",
    "extract_json_array",
    "DEFAULT_MODEL",
    "DEFAULT_BATCH_CHARS",
]

log = logging.getLogger("audiobook.markup")

# Бриф называет claude-sonnet-4-6; оставлено sonnet 5 по отдельной просьбе —
# он дешевле при том же качестве разметки. Меняется параметром model.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_BATCH_CHARS = 3000
DEFAULT_OVERLAP = 1
DEFAULT_MAX_TOKENS = 8000
DEFAULT_ATTEMPTS = 3

# Доля символов абзаца, которую разметка обязана покрыть. Ниже — считаем,
# что модель пересказала абзац, и берём исходный текст.
MIN_COVERAGE = 0.6

_NARRATOR_SYNONYMS = {
    "narrator", "рассказчик", "автор", "повествователь", "нарратор",
    "текст", "описание", "неизвестно", "unknown", "n/a", "-", "—", "",
}


class MarkupError(RuntimeError):
    """Базовая ошибка разметки."""


class MarkupConfigError(MarkupError):
    """Повтор не поможет: нет ключа или пакета, неверная модель.

    Обрывает весь прогон — иначе книга на тысячу батчей молча уехала бы
    в один голос.
    """


class BatchFailed(MarkupError):
    """Батч не дался за отведённые попытки. Остальная глава продолжается."""


class MarkupParseError(MarkupError):
    """Модель вернула не разбираемый JSON."""


@dataclass
class Issue:
    kind: str  # api | parse | coverage | uncovered | merge
    detail: str
    paragraphs: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.paragraphs is None:
            self.paragraphs = []

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "paragraphs": self.paragraphs}


@dataclass
class MarkupResult:
    segments: list[Segment]
    characters: list[str]
    issues: list[Issue]
    model: str = DEFAULT_MODEL
    usage: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = {}

    @property
    def ok(self) -> bool:
        return not any(i.kind in ("api", "parse") for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "characters": self.characters,
            "issues": [i.to_dict() for i in self.issues],
            "model": self.model,
            "usage": self.usage,
            "ok": self.ok,
        }


# --------------------------------------------------------------------------
# Сведение имён
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[\"'«»„“”.,:;!?()\[\]]+")
_MAX_NAME_LEN = 48


class CharacterRegistry:
    """Сводит написания одного персонажа к одному имени.

    Порядок: явный алиас -> точное совпадение -> нечёткое -> новый персонаж.
    """

    def __init__(
        self,
        known: Iterable[str] = (),
        aliases: dict[str, str] | None = None,
        threshold: float = 0.82,
    ) -> None:
        self.threshold = threshold
        self.order: list[str] = []
        self._by_norm: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        self.merges: list[tuple[str, str]] = []
        for raw, canonical in (aliases or {}).items():
            self.aliases[self._norm(raw)] = canonical
        for name in known:
            self.resolve(name)

    @staticmethod
    def _norm(name: str) -> str:
        s = _PUNCT.sub(" ", str(name or "")).strip().lower()
        s = s.replace("ё", "е").replace("’", "'")
        return re.sub(r"\s+", " ", s)

    def names(self) -> list[str]:
        return [n for n in self.order if n != NARRATOR]

    def resolve(self, raw: str) -> str:
        name = str(raw or "").strip()
        if len(name) > _MAX_NAME_LEN:
            name = name[:_MAX_NAME_LEN].rstrip()
        norm = self._norm(name)

        if norm in _NARRATOR_SYNONYMS:
            return NARRATOR
        if norm in self.aliases:
            return self.aliases[norm]
        if norm in self._by_norm:
            return self._by_norm[norm]

        match = self._fuzzy(norm)
        if match is not None:
            self._by_norm[norm] = match
            self.merges.append((name, match))
            log.info("имя %r сведено к %r", name, match)
            return match

        self._by_norm[norm] = name
        self.order.append(name)
        return name

    def _fuzzy(self, norm: str) -> str | None:
        """Ловит опечатки и разнобой («Рудеус» / «Рудэус»).

        Сокращения («Руди») так не ловятся — для них есть явные алиасы.
        Совпадение первой буквы отсекает разные имена с общим хвостом
        («Елена» и «Алёна»).
        """
        if len(norm) < 4:
            return None
        best, best_ratio = None, 0.0
        for other, canonical in self._by_norm.items():
            if canonical == NARRATOR or len(other) < 4 or other[0] != norm[0]:
                continue
            ratio = difflib.SequenceMatcher(None, norm, other).ratio()
            if ratio > best_ratio:
                best, best_ratio = canonical, ratio
        return best if best_ratio >= self.threshold else None


# --------------------------------------------------------------------------
# Промпт
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Ты — литературный редактор. Ты размечаешь текст книги для озвучки \
многоголосой аудиокнигой.

На вход ты получаешь пронумерованные абзацы одной главы. Твоя задача — \
разбить их на реплики: куски текста, которые читает один голос. Один абзац \
может дать несколько реплик.

Каждая реплика — объект с полями:
  "para"    — номер абзаца, ровно такой же, как во входных данных (целое число)
  "speaker" — "narrator" для слов автора, описаний и ремарок; иначе имя персонажа
  "text"    — текст реплики без обрамляющих кавычек и без тире прямой речи; \
слова не менять, не сокращать, не переписывать
  "emotion" — одно из: нейтрально, радостно, зло, грустно

Правила:
1. Ответ — СТРОГО JSON-массив. Без markdown-обёртки, без ```json, без \
пояснений до и после. Первый символ ответа — «[», последний — «]».
2. Ничего не выбрасывай: склеенные тексты реплик одного абзаца должны \
покрывать его целиком. Снять можно только кавычки прямой речи, тире в начале \
реплики и лишние пробелы.
3. Слова автора внутри прямой речи разделяй. «— Идём, — сказал Марк и встал.» \
даёт две реплики: «Идём» от Марка и «сказал Марк и встал» от narrator.
4. Имена бери из списка известных персонажей, если речь об одном и том же \
человеке. Не изобретай новых написаний для уже известного персонажа и не \
склоняй имя: speaker всегда в именительном падеже.
5. Если говорящего определить нельзя — "narrator".
6. Внутренний монолог от первого лица — реплика этого персонажа, если из \
текста ясно, чей он; иначе "narrator".
7. emotion ставь по самой реплике; по умолчанию "нейтрально".
8. Размечай только абзацы из раздела «АБЗАЦЫ». Абзацы из раздела «КОНТЕКСТ» \
в ответ не включай."""


def _user_message(
    book_title: str,
    chapter_label: str,
    paragraphs: Sequence[Paragraph],
    context: Sequence[Paragraph],
    context_segments: Sequence[Segment],
    known: Sequence[str],
) -> str:
    parts: list[str] = []
    if book_title:
        parts.append(f"Книга: {book_title}")
    parts.append(chapter_label)
    parts.append("")

    if known:
        parts.append("Известные персонажи (используй ровно эти написания):")
        parts.append(", ".join(known))
    else:
        parts.append("Известных персонажей пока нет — это начало книги.")
    parts.append("")

    if context:
        parts.append("КОНТЕКСТ (уже размечено, в ответ не включать):")
        for p in context:
            parts.append(f"[{p.index}] {p.text}")
        if context_segments:
            parts.append("Разметка контекста:")
            for s in context_segments:
                parts.append(f"{s.speaker}: {s.text}")
        parts.append("")

    parts.append("АБЗАЦЫ (разметить все):")
    for p in paragraphs:
        parts.append(f"[{p.index}] {p.text}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Батчи
# --------------------------------------------------------------------------


@dataclass
class Batch:
    context: list[Paragraph]
    paragraphs: list[Paragraph]


def build_batches(
    paragraphs: Sequence[Paragraph],
    max_chars: int = DEFAULT_BATCH_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Batch]:
    """Нарезать абзацы на батчи с перекрытием в ``overlap`` абзацев."""
    groups: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    size = 0

    for p in paragraphs:
        # Абзац длиннее лимита едет один: резать текст нельзя.
        if current and size + len(p.text) > max_chars:
            groups.append(current)
            current, size = [], 0
        current.append(p)
        size += len(p.text)
    if current:
        groups.append(current)

    out: list[Batch] = []
    for i, group in enumerate(groups):
        context = groups[i - 1][-overlap:] if i > 0 and overlap > 0 else []
        out.append(Batch(context=list(context), paragraphs=list(group)))
    return out


# --------------------------------------------------------------------------
# Разбор ответа
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_TRAILING_COMMA = re.compile(r",\s*([\]}])")


def extract_json_array(raw: str) -> list[dict[str, Any]]:
    """Достать JSON-массив из ответа, даже если модель добавила обёртку."""
    s = _FENCE.sub("", (raw or "").strip()).strip()
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end <= start:
        raise MarkupParseError(f"в ответе нет JSON-массива: {s[:200]!r}")
    body = s[start : end + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        try:
            data = json.loads(_TRAILING_COMMA.sub(r"\1", body))
        except json.JSONDecodeError as exc:
            raise MarkupParseError(f"JSON не разбирается: {exc}") from exc
    if not isinstance(data, list):
        raise MarkupParseError(f"ожидался массив, пришло {type(data).__name__}")
    return [x for x in data if isinstance(x, dict)]


def _normalize_emotion(value: Any) -> str:
    s = str(value or "").strip().lower().replace("ё", "е")
    for emotion in EMOTIONS:
        if s == emotion.replace("ё", "е"):
            return emotion
    return EMOTIONS[0]


def _locate(haystack: str, needle: str, start_at: int) -> tuple[int, int] | None:
    """Найти текст реплики в абзаце, чтобы редактор мог её подсветить."""
    if not needle:
        return None
    position = haystack.find(needle, start_at)
    if position == -1:
        position = haystack.find(needle)  # модель могла переставить куски
    if position == -1:
        return None
    return position, position + len(needle)


def _validate_batch(
    items: Sequence[dict[str, Any]],
    targets: Sequence[Paragraph],
    spans: dict[int, tuple[int, int]],
    registry: CharacterRegistry,
) -> tuple[list[Segment], list[Issue]]:
    """Свести ответ модели к сегментам, не потеряв ни одного абзаца."""
    by_index = {p.index: p for p in targets}
    grouped: dict[int, list[Segment]] = {p.index: [] for p in targets}
    issues: list[Issue] = []
    foreign: set[int] = set()

    for item in items:
        try:
            para = int(item.get("para"))
        except (TypeError, ValueError):
            continue
        if para not in grouped:
            foreign.add(para)
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        grouped[para].append(
            Segment(
                speaker=registry.resolve(item.get("speaker")),
                text=text,
                emotion=_normalize_emotion(item.get("emotion")),
            )
        )

    if foreign:
        issues.append(
            Issue("parse", "модель вернула абзацы не из батча", sorted(foreign))
        )

    segments: list[Segment] = []
    missing: list[int] = []
    thin: list[int] = []

    for paragraph in targets:
        found = grouped[paragraph.index]
        span = spans.get(paragraph.index)
        raw_text = paragraph.text

        if not found:
            missing.append(paragraph.index)
            found = [Segment(speaker=NARRATOR, text=raw_text)]
        else:
            covered = sum(len(s.text) for s in found)
            if raw_text and covered / len(raw_text) < MIN_COVERAGE:
                thin.append(paragraph.index)
                found = [Segment(speaker=NARRATOR, text=raw_text)]

        # Смещения в тексте главы — по ним редактор подсвечивает сегмент.
        cursor = 0
        for segment in found:
            local = _locate(raw_text, segment.text, cursor)
            if local and span:
                segment.char_start = span[0] + local[0]
                segment.char_end = span[0] + local[1]
                cursor = local[1]
            segments.append(segment)

    if missing:
        issues.append(Issue("uncovered", "абзац пропущен моделью", missing))
    if thin:
        issues.append(
            Issue(
                "coverage",
                f"разметка покрыла меньше {int(MIN_COVERAGE * 100)}% текста абзаца",
                thin,
            )
        )
    return segments, issues


# --------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------


def _import_anthropic():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise MarkupConfigError(
            "нужен пакет anthropic: pip install -r requirements.txt"
        ) from exc
    return anthropic


class Annotator:
    """Обёртка над Messages API: батчи, повторы, разбор ответа."""

    def __init__(
        self,
        client: Any = None,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        attempts: int = DEFAULT_ATTEMPTS,
        base_delay: float = 2.0,
    ) -> None:
        self._ah = _import_anthropic()
        if client is None:
            options: dict[str, Any] = {}
            if api_key:
                options["api_key"] = api_key
            if base_url:
                options["base_url"] = base_url
            try:
                client = self._ah.Anthropic(**options)
            except Exception as exc:  # noqa: BLE001
                raise MarkupConfigError(
                    f"не создать клиент Anthropic: {exc}"
                ) from exc
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.attempts = attempts
        self.base_delay = base_delay
        self.usage: dict[str, int] = {}

    def _add_usage(self, usage: Any) -> None:
        for key in (
            "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
        ):
            value = getattr(usage, key, None)
            if value:
                self.usage[key] = self.usage.get(key, 0) + int(value)

    def _request(self, user_message: str) -> list[dict[str, Any]]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Один и тот же промпт на все батчи книги — пусть кешируется.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        self._add_usage(getattr(response, "usage", None))

        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise BatchFailed(
                f"модель отказалась размечать батч: {getattr(response, 'stop_details', None)}"
            )
        if stop == "max_tokens":
            raise MarkupParseError("ответ обрезан по max_tokens — JSON неполный")

        raw = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        return extract_json_array(raw)

    def request(self, user_message: str, label: str) -> list[dict[str, Any]]:
        ah = self._ah
        fatal = (
            ah.AuthenticationError, ah.PermissionDeniedError,
            ah.NotFoundError, ah.BadRequestError,
        )
        last: Exception | None = None

        for attempt in range(1, self.attempts + 1):
            try:
                return self._request(user_message)
            except fatal as exc:
                raise MarkupConfigError(f"{label}: {exc}") from exc
            except TypeError as exc:
                if "authentication" not in str(exc).lower():
                    raise
                raise MarkupConfigError(
                    "не найден ключ Anthropic: задайте его в настройках"
                ) from exc
            except (
                ah.RateLimitError, ah.APIStatusError,
                ah.APIConnectionError, ah.APITimeoutError,
                MarkupParseError,
            ) as exc:
                last = exc
                if attempt == self.attempts:
                    break
                delay = self._backoff(exc, attempt)
                log.warning(
                    "%s: попытка %d/%d не удалась (%s), повтор через %.1f с",
                    label, attempt, self.attempts, type(exc).__name__, delay,
                )
                time.sleep(delay)

        raise BatchFailed(
            f"{label}: {self.attempts} попытки подряд неудачны: {last}"
        ) from last

    def _backoff(self, exc: Exception, attempt: int) -> float:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                after = float(response.headers.get("retry-after", ""))
                if after:
                    return after
            except (TypeError, ValueError):
                pass
        return self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)


# --------------------------------------------------------------------------
# Разметка главы
# --------------------------------------------------------------------------


def markup_text(
    text: str,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    base_url: str | None = None,
    batch_chars: int = DEFAULT_BATCH_CHARS,
    overlap: int = DEFAULT_OVERLAP,
    attempts: int = DEFAULT_ATTEMPTS,
    known: Sequence[str] = (),
    aliases: dict[str, str] | None = None,
    book_title: str = "",
    chapter_label: str = "",
    skip_paragraphs: Sequence[int] = (),
    on_progress: Any = None,
) -> MarkupResult:
    """Разметить текст главы.

    ``skip_paragraphs`` — абзацы, которые трогать нельзя (в них ручные
    правки). Они не попадают в запрос и не попадают в результат: вызывающий
    подставит на их место существующие сегменты.
    """
    spans = paragraph_spans(text)
    paragraphs = [
        Paragraph(index=i, text=text[start:end])
        for i, (start, end) in enumerate(spans)
    ]
    span_by_index = {i: span for i, span in enumerate(spans)}
    skip = set(skip_paragraphs)
    todo = [p for p in paragraphs if p.index not in skip]

    registry = CharacterRegistry(known=known, aliases=aliases)
    if not todo:
        return MarkupResult(segments=[], characters=registry.names(), issues=[], model=model)

    annotator = Annotator(
        client=client, model=model, api_key=api_key,
        base_url=base_url, attempts=attempts,
    )
    batches = build_batches(todo, batch_chars, overlap)
    segments: list[Segment] = []
    issues: list[Issue] = []

    for position, batch in enumerate(batches):
        label = (
            f"абзацы {batch.paragraphs[0].index}-{batch.paragraphs[-1].index}"
        )
        context_ids = {p.index for p in batch.context}
        context_segments = [
            s for s in segments[-4:]
            if s.char_start is not None
            and any(
                span_by_index[i][0] <= s.char_start < span_by_index[i][1]
                for i in context_ids
            )
        ]
        message = _user_message(
            book_title=book_title,
            chapter_label=chapter_label,
            paragraphs=batch.paragraphs,
            context=batch.context,
            context_segments=context_segments,
            known=registry.names(),
        )

        try:
            items = annotator.request(message, label)
        except BatchFailed as exc:
            # Батч потерян — абзацы уходят рассказчику, но не пропадают.
            log.error("%s: пропущен — %s", label, exc)
            issues.append(
                Issue("api", str(exc), [p.index for p in batch.paragraphs])
            )
            for paragraph in batch.paragraphs:
                span = span_by_index[paragraph.index]
                segments.append(
                    Segment(
                        speaker=NARRATOR,
                        text=paragraph.text,
                        char_start=span[0],
                        char_end=span[1],
                        error="батч не размечен",
                    )
                )
            continue

        batch_segments, batch_issues = _validate_batch(
            items, batch.paragraphs, span_by_index, registry
        )
        segments.extend(batch_segments)
        issues.extend(batch_issues)

        if on_progress is not None:
            on_progress(position + 1, len(batches))

    if registry.merges:
        issues.append(
            Issue(
                "merge",
                "; ".join(f"«{a}» → «{b}»" for a, b in registry.merges),
            )
        )

    segments.sort(key=lambda s: (s.char_start if s.char_start is not None else 1 << 30))
    return MarkupResult(
        segments=segments,
        characters=registry.names(),
        issues=issues,
        model=model,
        usage=dict(annotator.usage),
    )
