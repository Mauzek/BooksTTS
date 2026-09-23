"""Форматы помимо txt и epub: fb2, docx, pdf. Плюс общая сборка глав.

Все форматы сводятся к одному: поток блоков «заголовок» или «абзац». Дальше
главы режутся одинаково — по явным заголовкам (стиль в docx, ``<title>`` в
fb2) и по абзацам, похожим на «Глава N». Так автоопределение глав ведёт себя
предсказуемо в любом формате, а ошибки правятся вручную в библиотеке.
"""

from __future__ import annotations

import base64
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from .parser import (
    DEFAULT_CHAPTER_PATTERNS,
    Book,
    Chapter,
    Paragraph,
    ParserError,
    _compiled,
    _match_chapter_heading,
    normalize_text,
)

__all__ = [
    "HEADING",
    "PARAGRAPH",
    "chapters_from_blocks",
    "ensure_unique_numbers",
    "load_fb2",
    "load_docx",
    "load_pdf",
]

HEADING = "heading"
PARAGRAPH = "para"

Block = tuple[str, str]


# --------------------------------------------------------------------------
# Сборка глав из блоков
# --------------------------------------------------------------------------


def chapters_from_blocks(
    blocks: Iterable[Block],
    *,
    min_chapter_chars: int = 300,
    chapter_patterns: Sequence[str] | None = None,
) -> list[Chapter]:
    """Разрезать поток блоков на главы.

    Заголовок без единого абзаца после него («Часть первая» прямо перед
    «Главой 1») главой не становится. Текст до первого заголовка — глава,
    только если он длиннее ``min_chapter_chars``: иначе это титул или аннотация.
    """
    patterns = _compiled(chapter_patterns or DEFAULT_CHAPTER_PATTERNS)
    drafts: list[tuple[int | None, str, bool, list[str]]] = []  # номер, заголовок, явный, абзацы
    current: tuple[int | None, str, bool, list[str]] = (None, "", False, [])

    for kind, raw in blocks:
        text = normalize_text(raw)
        if not text:
            continue
        heading = _match_chapter_heading(text, patterns)
        if kind == HEADING or heading is not None:
            drafts.append(current)
            if heading is not None:
                current = (heading[0], heading[1], True, [])
            else:
                current = (None, text, True, [])
        else:
            current[3].append(text)
    drafts.append(current)

    chapters: list[Chapter] = []
    auto = 0
    for index, (number, title, explicit, paragraphs) in enumerate(drafts):
        if not paragraphs:
            continue
        n_chars = sum(len(p) for p in paragraphs)
        if index == 0 and not explicit and n_chars < min_chapter_chars:
            continue
        auto = number if number is not None else auto + 1
        chapters.append(
            Chapter(
                number=auto,
                title=title,
                paragraphs=[Paragraph(i, p) for i, p in enumerate(paragraphs)],
            )
        )
    return chapters


def ensure_unique_numbers(chapters: list[Chapter]) -> list[Chapter]:
    """Номера глав должны строго расти — в базе они уникальны внутри книги.

    «Часть 1. Глава 1 … Часть 2. Глава 1» даёт повторы. Тогда нумеруем подряд,
    а исходный номер сохраняем в заголовке, чтобы не потерять.
    """
    numbers = [chapter.number for chapter in chapters]
    if all(n >= 1 for n in numbers) and all(b > a for a, b in zip(numbers, numbers[1:])):
        return chapters
    for index, chapter in enumerate(chapters, start=1):
        if chapter.number != index and chapter.number >= 1:
            original = f"Глава {chapter.number}"
            if original.lower() not in chapter.title.lower():
                chapter.title = f"{original}. {chapter.title}" if chapter.title else original
        chapter.number = index
    return chapters


# --------------------------------------------------------------------------
# fb2
# --------------------------------------------------------------------------


def _xml_soup(data: bytes):
    from bs4 import BeautifulSoup

    for parser in ("lxml-xml", "xml", "html.parser"):
        try:
            return BeautifulSoup(data, parser)
        except Exception:  # noqa: BLE001 — нужного парсера может не быть
            continue
    raise ParserError("не удалось разобрать XML fb2")


def _read_fb2_bytes(path: Path) -> bytes:
    """fb2 бывает голым XML и бывает в zip (``.fb2.zip``)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".fb2")]
            if not members:
                raise ParserError(f"в архиве {path.name} нет файла .fb2")
            return archive.read(members[0])
    return path.read_bytes()


def _local(tag) -> str:
    return (tag.name or "").split(":")[-1].lower()


def _fb2_section_blocks(section, top_level: bool = False) -> Iterable[Block]:
    for child in section.find_all(recursive=False):
        name = _local(child)
        if name == "title":
            text = " ".join(p.get_text("", strip=True) for p in child.find_all(True) if _local(p) == "p")
            text = text or child.get_text(" ", strip=True)
            # Заголовок самого <body> — это название книги, а не глава.
            if not top_level and text:
                yield HEADING, text
        elif name == "section":
            yield from _fb2_section_blocks(child)
        elif name in ("p", "subtitle", "v", "text-author"):
            yield PARAGRAPH, child.get_text("", strip=True)
        elif name in ("epigraph", "cite", "poem", "stanza"):
            yield from _fb2_section_blocks(child)
        # empty-line, image, table — для озвучки нечего


def load_fb2(path: Path, *, min_chapter_chars: int = 300) -> Book:
    soup = _xml_soup(_read_fb2_bytes(path))

    title_info = soup.find(lambda t: _local(t) == "title-info")
    title, author = "", ""
    cover_id = ""
    if title_info is not None:
        book_title = title_info.find(lambda t: _local(t) == "book-title")
        title = book_title.get_text(" ", strip=True) if book_title else ""
        author_tag = title_info.find(lambda t: _local(t) == "author")
        if author_tag is not None:
            names = [
                author_tag.find(lambda t, n=n: _local(t) == n)
                for n in ("first-name", "middle-name", "last-name")
            ]
            author = " ".join(n.get_text(strip=True) for n in names if n and n.get_text(strip=True))
        coverpage = title_info.find(lambda t: _local(t) == "coverpage")
        image = coverpage.find(lambda t: _local(t) == "image") if coverpage else None
        if image is not None:
            href = next((v for k, v in image.attrs.items() if k.split(":")[-1] == "href"), "")
            cover_id = href.lstrip("#")

    bodies = [
        body for body in soup.find_all(lambda t: _local(t) == "body")
        if (body.get("name") or "").lower() not in ("notes", "comments")
    ]
    if not bodies:
        raise ParserError(f"в {path.name} нет <body> с текстом")

    blocks: list[Block] = []
    for body in bodies:
        blocks.extend(_fb2_section_blocks(body, top_level=True))
    chapters = chapters_from_blocks(blocks, min_chapter_chars=min_chapter_chars)
    if not chapters:
        raise ParserError(f"в {path.name} не нашлось текста глав")

    book = Book(title=title or path.stem, author=author, path=path, chapters=chapters)
    if cover_id:
        binary = soup.find(lambda t: _local(t) == "binary" and t.get("id") == cover_id)
        if binary is not None:
            try:
                book.cover = base64.b64decode(binary.get_text(strip=True))
                book.cover_type = _image_ext(binary.get("content-type", ""), cover_id)
            except (ValueError, TypeError):
                pass
    return book


def _image_ext(content_type: str, name: str = "") -> str:
    content_type = (content_type or "").lower()
    if "png" in content_type or name.lower().endswith(".png"):
        return "png"
    if "webp" in content_type or name.lower().endswith(".webp"):
        return "webp"
    return "jpg"


# --------------------------------------------------------------------------
# docx
# --------------------------------------------------------------------------

_HEADING_STYLES = re.compile(r"^(heading|заголовок)\s*[12]$|^title$|^название$", re.IGNORECASE)


def load_docx(path: Path, *, min_chapter_chars: int = 300) -> Book:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise ParserError("для .docx нужен python-docx: pip install -r requirements.txt") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 — битый архив
        raise ParserError(f"не удалось открыть {path.name}: {exc}") from exc

    blocks: list[Block] = []
    for paragraph in document.paragraphs:
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        kind = HEADING if _HEADING_STYLES.match(style.strip()) else PARAGRAPH
        blocks.append((kind, paragraph.text))

    chapters = chapters_from_blocks(blocks, min_chapter_chars=min_chapter_chars)
    if not chapters:
        raise ParserError(f"в {path.name} не нашлось текста")
    props = document.core_properties
    return Book(
        title=(props.title or "").strip() or path.stem,
        author=(props.author or "").strip(),
        path=path,
        chapters=chapters,
    )


# --------------------------------------------------------------------------
# pdf
# --------------------------------------------------------------------------

_SENTENCE_END = tuple(".!?…:;»\"”)")
_PAGE_NUMBER = re.compile(r"^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$")


def _pdf_lines(reader) -> list[list[str]]:
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — одна битая страница не роняет книгу
            text = ""
        pages.append([line.rstrip() for line in text.replace("\r", "").split("\n")])
    return pages


def _running_headers(pages: Sequence[Sequence[str]]) -> set[str]:
    """Колонтитулы: одна и та же строка вверху или внизу у многих страниц."""
    if len(pages) < 4:
        return set()
    counter: Counter[str] = Counter()
    for lines in pages:
        edge = [l.strip() for l in (list(lines[:2]) + list(lines[-2:])) if l.strip()]
        counter.update(set(edge))
    threshold = max(3, len(pages) // 3)
    return {line for line, n in counter.items() if n >= threshold and len(line) < 80}


def pdf_blocks(pages: Sequence[Sequence[str]]) -> list[Block]:
    """Склеить строки pdf обратно в абзацы.

    В pdf нет абзацев — только строки вёрстки. Новый абзац начинается после
    пустой строки, а также когда прошлая строка закончила предложение, а эта
    начинается с заглавной или с тире реплики. Перенос «сло-/во» склеивается.
    """
    headers = _running_headers(pages)
    patterns = _compiled(DEFAULT_CHAPTER_PATTERNS)
    blocks: list[Block] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            blocks.append((PARAGRAPH, buffer.strip()))
        buffer = ""

    for lines in pages:
        for raw in lines:
            line = raw.strip()
            if not line:
                flush()
                continue
            if line in headers or _PAGE_NUMBER.match(line):
                continue
            # «Глава 1» обычно стоит отдельной строкой без точки — иначе она
            # приклеилась бы к первому абзацу главы.
            if _match_chapter_heading(line, patterns) is not None:
                flush()
                blocks.append((HEADING, line))
                continue
            starts_new = line[0].isupper() or line[0] in "—–-«\""
            if buffer and buffer.rstrip().endswith(_SENTENCE_END) and starts_new:
                flush()
            if buffer.endswith("-") and line[0].islower():
                buffer = buffer[:-1] + line
            else:
                buffer = f"{buffer} {line}" if buffer else line
    flush()
    return blocks


def load_pdf(path: Path, *, min_chapter_chars: int = 300) -> Book:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ParserError("для .pdf нужен pypdf: pip install -r requirements.txt") from exc
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ParserError(f"не удалось открыть {path.name}: {exc}") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ParserError(f"{path.name} защищён паролем") from exc

    chapters = chapters_from_blocks(pdf_blocks(_pdf_lines(reader)), min_chapter_chars=min_chapter_chars)
    if not chapters:
        raise ParserError(
            f"в {path.name} не нашлось текста — возможно, это скан без текстового слоя"
        )
    meta = reader.metadata or {}
    return Book(
        title=str(getattr(meta, "title", "") or "").strip() or path.stem,
        author=str(getattr(meta, "author", "") or "").strip(),
        path=path,
        chapters=chapters,
    )
