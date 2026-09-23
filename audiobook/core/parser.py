"""Чтение книги, разбивка на главы и абзацы.

Форматы:

* ``.txt``  — главы ищутся по маркерам вида «Глава 12», «ГЛАВА XIV»,
  «Глава третья», «Chapter 5». Абзацы — блоки, разделённые пустой строкой;
  если пустых строк в файле нет, абзацем считается строка.
* ``.epub`` — ebooklib + BeautifulSoup. Порядок глав берётся из spine,
  заголовки — из ``<h1>``..``<h3>`` или из оглавления.
* ``.fb2``, ``.docx``, ``.pdf`` — в :mod:`formats`.

Модуль ничего не скачивает и не отправляет наружу: только локальный файл.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "Paragraph",
    "Chapter",
    "Book",
    "ParserError",
    "load_book",
    "parse_chapter_range",
    "paragraphs_from_text",
    "chapter_from_text",
    "paragraph_spans",
    "chapter_text",
]


class ParserError(RuntimeError):
    """Входной файл не читается или не разбирается."""


# --------------------------------------------------------------------------
# Модель книги
# --------------------------------------------------------------------------


@dataclass
class Paragraph:
    """Один абзац главы."""

    index: int  # сквозной номер внутри главы, с 0
    text: str

    def __len__(self) -> int:  # удобно для батчинга в roles.py
        return len(self.text)


@dataclass
class Chapter:
    number: int
    title: str
    paragraphs: list[Paragraph] = field(default_factory=list)
    source_id: str = ""  # href внутри epub, для отладки

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.paragraphs)

    @property
    def n_chars(self) -> int:
        return sum(len(p.text) for p in self.paragraphs)

    @property
    def label(self) -> str:
        return f"Глава {self.number}" + (f". {self.title}" if self.title else "")


@dataclass
class Book:
    title: str
    author: str
    path: Path
    chapters: list[Chapter] = field(default_factory=list)
    cover: bytes | None = None  # картинка обложки, если она есть в файле
    cover_type: str = ""  # расширение: jpg | png | webp

    @property
    def n_chars(self) -> int:
        return sum(ch.n_chars for ch in self.chapters)

    def chapter(self, number: int) -> Chapter:
        for ch in self.chapters:
            if ch.number == number:
                return ch
        raise KeyError(f"главы {number} нет в книге (есть {self.chapter_numbers()})")

    def chapter_numbers(self) -> list[int]:
        return [ch.number for ch in self.chapters]

    def select(self, numbers: Iterable[int] | None) -> list[Chapter]:
        """Главы по списку номеров; ``None`` — все главы."""
        if numbers is None:
            return list(self.chapters)
        wanted = set(numbers)
        return [ch for ch in self.chapters if ch.number in wanted]


# --------------------------------------------------------------------------
# Нормализация текста
# --------------------------------------------------------------------------

# Абзацы в тексте главы всегда разделены пустой строкой: по этому
# разделителю считаются смещения для подсветки в редакторе.
PARAGRAPH_SEP = "\n\n"

_SPACES = re.compile(r"[ \t  -   ]+")
_SOFT = re.compile(r"[­​﻿]")
_DIALOGUE_DASH = re.compile(r"^[\-‐‑‒–―]\s")
# Дефис, окружённый пробелами, — это тире, а не часть слова («что-то» цело).
_INLINE_DASH = re.compile(r"(?<=\s)[\-‐‑‒–―](?=\s)")


def normalize_text(raw: str) -> str:
    """Схлопнуть пробелы, убрать мягкие переносы, привести тире к «—»."""
    s = unicodedata.normalize("NFC", raw)
    s = _SOFT.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("--", "—")
    s = _SPACES.sub(" ", s)
    s = s.strip()
    # Реплику часто начинают дефисом или коротким тире — озвучке и разметке
    # проще, когда это всегда длинное тире.
    s = _DIALOGUE_DASH.sub("— ", s)
    s = _INLINE_DASH.sub("—", s)
    return s


# --------------------------------------------------------------------------
# Номера глав
# --------------------------------------------------------------------------

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

_ORDINAL_WORDS = {
    "первая": 1, "вторая": 2, "третья": 3, "четвёртая": 4, "четвертая": 4,
    "пятая": 5, "шестая": 6, "седьмая": 7, "восьмая": 8, "девятая": 9,
    "десятая": 10, "одиннадцатая": 11, "двенадцатая": 12, "тринадцатая": 13,
    "четырнадцатая": 14, "пятнадцатая": 15, "шестнадцатая": 16,
    "семнадцатая": 17, "восемнадцатая": 18, "девятнадцатая": 19,
    "двадцатая": 20,
}

# Маркер главы: слово, номер, необязательный заголовок в той же строке.
DEFAULT_CHAPTER_PATTERNS: tuple[str, ...] = (
    r"^\s*(?:глава|chapter)\s+(?P<num>[^\s.:—–\-]{1,16})\s*[.:—–\-]?\s*(?P<title>.{0,120}?)\s*$",
)

_MAX_HEADING_LEN = 160


def _roman_to_int(s: str) -> int | None:
    s = s.lower()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = _ROMAN[c]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total or None


def _parse_chapter_number(token: str) -> int | None:
    token = token.strip().strip(".:—–-").lower()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[token]
    return _roman_to_int(token)


def _match_chapter_heading(line: str, patterns: Sequence[re.Pattern[str]]):
    """-> (номер | None, заголовок), если строка похожа на заголовок главы."""
    if not line or len(line) > _MAX_HEADING_LEN:
        return None
    for pat in patterns:
        m = pat.match(line)
        if not m:
            continue
        raw_num = m.group("num") or ""
        num = _parse_chapter_number(raw_num)
        title = (m.groupdict().get("title") or "").strip(" .:—–-")
        if num is None:
            # «Глава» без распознаваемого номера — всё ещё заголовок,
            # номер проставим по порядку.
            title = (raw_num + " " + title).strip(" .:—–-")
        return num, title
    return None


def parse_chapter_range(spec: str) -> list[int]:
    """``"1-5,8,11-12"`` -> ``[1, 2, 3, 4, 5, 8, 11, 12]``."""
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part.lstrip("-"):
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ParserError(f"не разобрать диапазон глав: {part!r}") from exc
            if hi < lo:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part))
            except ValueError as exc:
                raise ParserError(f"не разобрать номер главы: {part!r}") from exc
    return sorted(dict.fromkeys(out))


# --------------------------------------------------------------------------
# .txt
# --------------------------------------------------------------------------

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "koi8-r", "utf-16")


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ParserError(
        f"не удалось определить кодировку {path.name}; "
        f"пробовали: {', '.join(_ENCODINGS)}"
    )


def _split_paragraphs(lines: Sequence[str], blank_separated: bool) -> list[str]:
    """Собрать абзацы из сырых строк одной главы."""
    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            text = normalize_text(" ".join(buf))
            if text:
                paragraphs.append(text)
            buf.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if blank_separated:
            buf.append(stripped)
        else:
            # Каждая непустая строка — отдельный абзац.
            text = normalize_text(stripped)
            if text:
                paragraphs.append(text)
    flush()
    return paragraphs


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Границы абзацев в уже нормализованном тексте главы.

    Текст главы хранится в базе нормализованным и разделённым пустой строкой,
    поэтому смещения точные — по ним редактор подсвечивает сегменты.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    for block in (text or "").split(PARAGRAPH_SEP):
        stripped = block.strip()
        if stripped:
            start = text.index(stripped, position)
            spans.append((start, start + len(stripped)))
            position = start + len(stripped)
        else:
            position += len(block) + 2
    return spans


def chapter_text(paragraphs: Sequence[Paragraph | str]) -> str:
    """Канонический текст главы: нормализованные абзацы через пустую строку."""
    out = []
    for item in paragraphs:
        value = item.text if isinstance(item, Paragraph) else str(item)
        value = normalize_text(value)
        if value:
            out.append(value)
    return PARAGRAPH_SEP.join(out)


def paragraphs_from_text(text: str) -> list[Paragraph]:
    """Разбить сырой текст на абзацы — для текста, вставленного руками.

    Если в тексте есть пустые строки, абзац — блок между ними; иначе абзацем
    считается строка.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    blank_separated = "\n\n" in normalized
    return [
        Paragraph(i, t)
        for i, t in enumerate(_split_paragraphs(lines, blank_separated))
    ]


def chapter_from_text(text: str, number: int = 1, title: str = "") -> Chapter:
    """Собрать главу из вставленного текста, сняв заголовок «Глава N», если он есть."""
    paragraphs = paragraphs_from_text(text)
    if paragraphs:
        heading = _match_chapter_heading(
            paragraphs[0].text, _compiled(DEFAULT_CHAPTER_PATTERNS)
        )
        if heading is not None:
            number = heading[0] or number
            title = title or heading[1]
            paragraphs = [
                Paragraph(i, p.text) for i, p in enumerate(paragraphs[1:])
            ]
    return Chapter(number=number, title=title, paragraphs=paragraphs)


def _load_txt(
    path: Path,
    *,
    chapter_patterns: Sequence[re.Pattern[str]],
    min_chapter_chars: int,
) -> Book:
    raw = read_text_file(path)
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blank_separated = raw.count("\n\n") >= 3

    # (номер | None, заголовок, строки тела)
    blocks: list[tuple[int | None, str, list[str]]] = []
    current: tuple[int | None, str, list[str]] = (None, "", [])

    for line in lines:
        heading = _match_chapter_heading(line.strip(), chapter_patterns)
        if heading is not None:
            blocks.append(current)
            current = (heading[0], heading[1], [])
        else:
            current[2].append(line)
    blocks.append(current)

    chapters: list[Chapter] = []
    auto_number = 0
    for num, title, body in blocks:
        paragraphs = _split_paragraphs(body, blank_separated)
        if not paragraphs:
            continue
        n_chars = sum(len(p) for p in paragraphs)
        if num is None and not title and not chapters and n_chars < min_chapter_chars:
            # Обложка/аннотация перед первой «Главой N» — не глава.
            continue
        auto_number = num if num is not None else auto_number + 1
        chapters.append(
            Chapter(
                number=auto_number,
                title=title,
                paragraphs=[Paragraph(i, t) for i, t in enumerate(paragraphs)],
            )
        )

    if not chapters:
        raise ParserError(f"в {path.name} не нашлось ни одного абзаца текста")

    return Book(title=path.stem, author="", path=path, chapters=chapters)


# --------------------------------------------------------------------------
# .epub
# --------------------------------------------------------------------------

_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "pre")
_DROP_TAGS = ("script", "style", "sup", "nav")


def _soup(html: bytes):
    from bs4 import BeautifulSoup

    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:  # noqa: BLE001 — lxml может быть не установлен
            continue
    raise ParserError("не удалось разобрать XHTML: нет ни lxml, ни html.parser")


def _toc_titles(book) -> dict[str, str]:
    """href (без якоря) -> заголовок из оглавления."""
    titles: dict[str, str] = {}

    def walk(node) -> None:
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
            return
        href = getattr(node, "href", None)
        title = getattr(node, "title", None)
        if href and title:
            titles.setdefault(href.split("#")[0], title.strip())

    walk(getattr(book, "toc", []) or [])
    return titles


def _spine_items(book):
    from ebooklib import ITEM_DOCUMENT

    items = []
    for entry in getattr(book, "spine", []) or []:
        idref = entry[0] if isinstance(entry, (list, tuple)) else entry
        item = book.get_item_with_id(idref)
        if item is not None:
            items.append(item)
    if not items:  # spine пуст или нестандартен — берём все документы подряд
        items = list(book.get_items_of_type(ITEM_DOCUMENT))
    return items


def _load_epub(path: Path, *, min_chapter_chars: int) -> Book:
    try:
        from ebooklib import epub
    except ImportError as exc:  # pragma: no cover
        raise ParserError(
            "для .epub нужны ebooklib и beautifulsoup4: pip install -r requirements.txt"
        ) from exc

    try:
        src = epub.read_epub(str(path))
    except Exception as exc:  # noqa: BLE001 — ebooklib кидает что угодно
        raise ParserError(f"не удалось открыть {path.name}: {exc}") from exc

    def meta(field_name: str) -> str:
        values = src.get_metadata("DC", field_name) or []
        return values[0][0].strip() if values and values[0] and values[0][0] else ""

    toc = _toc_titles(src)
    patterns = _compiled(DEFAULT_CHAPTER_PATTERNS)
    chapters: list[Chapter] = []

    for item in _spine_items(src):
        href = getattr(item, "file_name", "") or getattr(item, "href", "")
        try:
            soup = _soup(item.get_content())
        except ParserError:
            raise
        except Exception:  # noqa: BLE001 — битый документ не должен ронять книгу
            continue

        for tag in soup(list(_DROP_TAGS)):
            tag.decompose()

        blocks = soup.find_all(_BLOCK_TAGS)
        if blocks:
            texts = [normalize_text(b.get_text(" ", strip=True)) for b in blocks]
        else:  # вёрстка без <p> — режем по переводам строк
            texts = [
                normalize_text(line)
                for line in soup.get_text("\n", strip=True).split("\n")
            ]
        texts = [t for t in texts if t]
        if not texts:
            continue

        # Заголовок: <h1>..<h3>, иначе оглавление.
        title = ""
        number: int | None = None
        heading_tag = soup.find(["h1", "h2", "h3"])
        if heading_tag:
            title = normalize_text(heading_tag.get_text(" ", strip=True))
        if not title:
            title = toc.get(href, "")
        if title and texts and texts[0] == title:
            texts = texts[1:]

        parsed = _match_chapter_heading(title, patterns)
        if parsed is not None:
            number, inner = parsed
            title = inner or title

        n_chars = sum(len(t) for t in texts)
        if n_chars < min_chapter_chars and number is None:
            continue  # титул, копирайт, оглавление

        chapters.append(
            Chapter(
                number=number or 0,
                title=title,
                paragraphs=[Paragraph(i, t) for i, t in enumerate(texts)],
                source_id=href,
            )
        )

    if not chapters:
        raise ParserError(
            f"в {path.name} не нашлось глав длиннее {min_chapter_chars} символов"
        )

    # Проставляем номера там, где их не было в заголовках.
    next_num = 0
    for ch in chapters:
        if ch.number and ch.number > next_num:
            next_num = ch.number
        else:
            next_num += 1
            ch.number = next_num

    cover, cover_type = _epub_cover(src)
    return Book(
        title=meta("title") or path.stem,
        author=meta("creator"),
        path=path,
        chapters=chapters,
        cover=cover,
        cover_type=cover_type,
    )


def _epub_cover(src) -> tuple[bytes | None, str]:
    """Обложка epub: явная (EPUB3), по <meta name="cover"> (EPUB2) или по имени файла."""
    try:
        from ebooklib import ITEM_COVER, ITEM_IMAGE
    except ImportError:  # pragma: no cover
        return None, ""

    candidates = list(src.get_items_of_type(ITEM_COVER))
    for _value, attrs in src.get_metadata("OPF", "cover") or []:
        item = src.get_item_with_id((attrs or {}).get("content", ""))
        if item is not None:
            candidates.append(item)
    candidates += [
        item for item in src.get_items_of_type(ITEM_IMAGE)
        if "cover" in (item.get_name() or "").lower()
    ]
    for item in candidates:
        data = item.get_content()
        if data:
            name = (item.get_name() or "").lower()
            ext = "png" if name.endswith(".png") else "webp" if name.endswith(".webp") else "jpg"
            return data, ext
    return None, ""


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------


def _compiled(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]


def load_book(
    path: str | Path,
    *,
    chapter_patterns: Sequence[str] | None = None,
    min_chapter_chars: int = 300,
) -> Book:
    """Прочитать книгу любого поддерживаемого формата и вернуть :class:`Book`.

    :param chapter_patterns: регулярки заголовков глав (только для .txt);
        нужны именованные группы ``num`` и ``title``.
    :param min_chapter_chars: куски короче — служебные страницы, пропускаем.

    Номера глав в результате всегда строго растут: в библиотеке они уникальны.
    """
    from . import formats

    path = Path(path)
    if not path.exists():
        raise ParserError(f"файл не найден: {path}")

    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix == ".epub":
        book = _load_epub(path, min_chapter_chars=min_chapter_chars)
    elif suffix in (".txt", ".text", ".md"):
        book = _load_txt(
            path,
            chapter_patterns=_compiled(chapter_patterns or DEFAULT_CHAPTER_PATTERNS),
            min_chapter_chars=min_chapter_chars,
        )
    elif suffix == ".fb2" or name.endswith(".fb2.zip"):
        book = formats.load_fb2(path, min_chapter_chars=min_chapter_chars)
    elif suffix == ".docx":
        book = formats.load_docx(path, min_chapter_chars=min_chapter_chars)
    elif suffix == ".pdf":
        book = formats.load_pdf(path, min_chapter_chars=min_chapter_chars)
    else:
        raise ParserError(
            f"неподдерживаемый формат: {suffix or path.name} "
            f"(поддерживаются: {', '.join(SUPPORTED_EXTENSIONS)})"
        )
    formats.ensure_unique_numbers(book.chapters)
    return book


SUPPORTED_EXTENSIONS = (".txt", ".epub", ".fb2", ".fb2.zip", ".docx", ".pdf")
