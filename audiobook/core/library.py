"""Библиотека: импорт книг, дерево, структура глав, поиск, разметка.

Исходник копируется внутрь библиотеки, а в базу пишется относительный путь:
папку с библиотекой можно перенести на другую машину целиком.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from .. import paths
from . import repo, secrets
from .markup import DEFAULT_BATCH_CHARS, DEFAULT_MODEL, MarkupResult, markup_text
from .models import Book, Chapter, Segment
from .parser import PARAGRAPH_SEP, chapter_from_text, chapter_text, load_book, paragraph_spans

__all__ = [
    "LibraryError",
    "import_book",
    "delete_book",
    "library_tree",
    "book_view",
    "split_chapter",
    "merge_with_next",
    "add_chapters_from_file",
    "add_chapter_from_text",
    "remove_chapter",
    "set_cover",
    "remove_cover",
    "search_library",
    "markup_chapter",
    "chapter_view",
    "book_audio_dir",
]

Conn = sqlite3.Connection


class LibraryError(RuntimeError):
    """Книгу не импортировать, главу не разрезать или не разметить."""


def _unique_name(directory: Path, name: str) -> Path:
    """Имя файла, не затирающее уже лежащий в библиотеке."""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(2, 1000):
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise LibraryError(f"не подобрать имя для {name}")


def book_audio_dir(book_id: int) -> Path:
    """Папка с озвучкой книги."""
    return paths.audio_dir() / f"book-{book_id}"


# --------------------------------------------------------------------------
# Импорт и удаление
# --------------------------------------------------------------------------


def import_book(
    conn: Conn,
    source: str | Path,
    folder_id: int | None = None,
    title: str = "",
    copy_source: bool = True,
    min_chapter_chars: int = 300,
) -> Book:
    """Разобрать книгу и положить её с главами и обложкой в базу."""
    source = Path(source)
    parsed = load_book(source, min_chapter_chars=min_chapter_chars)

    stored_path = ""
    cover_path = None
    created: list[Path] = []
    try:
        if copy_source:
            paths.ensure_layout()
            target = _unique_name(paths.books_dir(), source.name)
            shutil.copy2(source, target)
            created.append(target)
            stored_path = paths.relative(target)
            if parsed.cover:
                cover = _unique_name(
                    paths.books_dir(), f"{target.stem}.cover.{parsed.cover_type or 'jpg'}"
                )
                cover.write_bytes(parsed.cover)
                created.append(cover)
                cover_path = paths.relative(cover)

        book = repo.create_book(
            conn,
            title=title or parsed.title,
            author=parsed.author,
            source_path=stored_path,
            folder_id=folder_id,
            cover_path=cover_path,
        )
        for chapter in parsed.chapters:
            repo.create_chapter(
                conn,
                book_id=book.id,
                number=chapter.number,
                title=chapter.title,
                # Канонический нормализованный текст: по нему считаются смещения.
                text=chapter_text(chapter.paragraphs),
            )
    except Exception:
        # Запись в базу откатится, а копия осталась бы без хозяина: следующий
        # импорт того же файла получил бы суффикс «-2» рядом с мусором.
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return book


def delete_book(conn: Conn, book_id: int) -> None:
    """Удалить книгу из базы и её файлы из библиотеки.

    Файлы удаляются только внутри библиотеки: путь из базы проходит через
    :func:`paths.absolute`, который не выпускает наружу.
    """
    book = repo.get_book(conn, book_id)
    repo.delete_book(conn, book_id)
    for relative in (book.source_path, book.cover_path):
        if relative:
            try:
                paths.absolute(relative).unlink(missing_ok=True)
            except (paths.PathsError, OSError):
                pass
    shutil.rmtree(book_audio_dir(book_id), ignore_errors=True)


# --------------------------------------------------------------------------
# Дерево и карточка книги
# --------------------------------------------------------------------------


def library_tree(conn: Conn) -> dict[str, Any]:
    """Папки с вложенностью, в каждой — её книги. Книги без папки — в корне."""
    books = repo.book_summaries(conn)

    def books_in(folder_id: int | None) -> list[dict[str, Any]]:
        return [books[b.id] for b in repo.list_books(conn, folder_id)]

    def walk(parent_id: int | None) -> list[dict[str, Any]]:
        return [
            {**folder.to_dict(), "folders": walk(folder.id), "books": books_in(folder.id)}
            for folder in repo.list_folders(conn, parent_id)
        ]

    return {"folders": walk(None), "books": books_in(None)}


def book_view(conn: Conn, book_id: int) -> dict[str, Any]:
    book = repo.get_book(conn, book_id)
    stats = repo.chapter_stats(conn, book_id)
    slots = repo.role_slots(conn, book_id)
    return {
        "book": book.to_dict(),
        "chapters": [
            {**chapter.to_dict(with_text=False), **stats.get(chapter.id, {})}
            for chapter in repo.list_chapters(conn, book_id)
        ],
        "speakers": [
            {"name": name, "count": count, "slot": slots[name]}
            for name, count in repo.book_speakers(conn, book_id).items()
        ],
        "playback": repo.get_playback(conn, book_id),
    }


# --------------------------------------------------------------------------
# Границы глав
# --------------------------------------------------------------------------


def split_chapter(conn: Conn, chapter_id: int, offset: int, title: str = "") -> tuple[Chapter, Chapter]:
    """Начать новую главу с абзаца, в котором стоит ``offset``.

    Граница всегда по началу абзаца: резать главу посреди предложения
    автоопределение не умеет, и человеку это тоже не нужно. Разметка
    переезжает вместе с текстом, смещения пересчитываются.
    """
    chapter = repo.get_chapter(conn, chapter_id)
    starts = [start for start, _ in paragraph_spans(chapter.text)]
    if len(starts) < 2:
        raise LibraryError("в главе один абзац — делить нечего")
    boundary = max((s for s in starts if s <= offset), default=starts[0])
    if boundary == starts[0]:
        raise LibraryError("это первый абзац главы — новая глава с него уже начинается")

    crossing = [
        s for s in repo.list_segments(conn, chapter_id)
        if s.char_start is not None and s.char_start < boundary < s.char_end
    ]
    if crossing:
        raise LibraryError(
            f"сегмент #{crossing[0].order} переходит через границу абзаца — "
            "сначала разделите его в редакторе разметки"
        )

    head = chapter.text[:boundary].rstrip()
    tail = chapter.text[boundary:]
    repo.shift_chapter_numbers(conn, chapter.book_id, chapter.number, 1)
    created = repo.create_chapter(
        conn, book_id=chapter.book_id, number=chapter.number + 1, title=title.strip(), text=tail
    )
    repo.move_segments(conn, chapter.id, created.id, boundary, -boundary)
    repo.update_chapter(conn, chapter.id, text=head)
    for target in (chapter.id, created.id):
        repo.renumber_segments(conn, target)
    repo.reset_chapter_derivatives(conn, [chapter.id, created.id])
    return repo.get_chapter(conn, chapter.id), repo.get_chapter(conn, created.id)


def merge_with_next(conn: Conn, chapter_id: int) -> Chapter:
    """Склеить главу со следующей: текст, разметка и озвучка реплик сохраняются."""
    chapter = repo.get_chapter(conn, chapter_id)
    following = repo.next_chapter(conn, chapter)
    if following is None:
        raise LibraryError("это последняя глава — склеивать не с чем")

    separator = PARAGRAPH_SEP if chapter.text and following.text else ""
    shift = len(chapter.text) + len(separator)
    repo.move_segments(conn, following.id, chapter.id, 0, shift)
    repo.update_chapter(conn, chapter.id, text=chapter.text + separator + following.text)
    repo.delete_chapter(conn, following.id)
    repo.shift_chapter_numbers(conn, chapter.book_id, following.number, -1)
    repo.renumber_segments(conn, chapter.id)
    repo.reset_chapter_derivatives(conn, [chapter.id])
    return repo.get_chapter(conn, chapter.id)


# --------------------------------------------------------------------------
# Главы: добавить в готовую книгу, удалить
# --------------------------------------------------------------------------


def _insert_chapters(
    conn: Conn, book_id: int, chapters: Sequence[tuple[str, str]], after: int | None
) -> list[Chapter]:
    """Вставить главы после главы номер ``after`` (None — в конец книги)."""
    existing = repo.list_chapters(conn, book_id)
    last = existing[-1].number if existing else 0
    anchor = last if after is None else max(0, min(int(after), last))
    if anchor < last:
        repo.shift_chapter_numbers(conn, book_id, anchor, len(chapters))
    return [
        repo.create_chapter(conn, book_id=book_id, number=anchor + i, title=title, text=text)
        for i, (title, text) in enumerate(chapters, start=1)
    ]


def add_chapters_from_file(
    conn: Conn, book_id: int, source: str | Path, after: int | None = None,
    min_chapter_chars: int = 300,
) -> list[Chapter]:
    """Дописать в книгу главы из файла любого поддерживаемого формата.

    Файл без заголовков глав станет одной главой с именем файла.
    """
    repo.get_book(conn, book_id)
    source = Path(source)
    parsed = load_book(source, min_chapter_chars=min_chapter_chars)
    chapters = [
        (chapter.title, chapter_text(chapter.paragraphs))
        for chapter in parsed.chapters
    ]
    chapters = [(title, text) for title, text in chapters if text.strip()]
    if not chapters:
        raise LibraryError(f"в файле {source.name} не нашлось текста")
    if len(chapters) == 1 and not chapters[0][0]:
        stem = source.name.split(".")[0]
        chapters = [(stem, chapters[0][1])]
    return _insert_chapters(conn, book_id, chapters, after)


def add_chapter_from_text(
    conn: Conn, book_id: int, text: str, title: str = "", after: int | None = None
) -> Chapter:
    """Дописать главу из вставленного текста. «Глава 5. …» в первой строке станет названием."""
    repo.get_book(conn, book_id)
    parsed = chapter_from_text(text, title=title.strip())
    body = chapter_text(parsed.paragraphs)
    if not body.strip():
        raise LibraryError("вставьте текст главы — пока он пустой")
    return _insert_chapters(conn, book_id, [(parsed.title.strip(), body)], after)[0]


def remove_chapter(conn: Conn, chapter_id: int) -> None:
    """Удалить главу с разметкой и озвучкой; следующие главы сдвинутся вверх."""
    chapter = repo.get_chapter(conn, chapter_id)
    if len(repo.list_chapters(conn, chapter.book_id)) <= 1:
        raise LibraryError("это единственная глава — удалите книгу целиком")
    audio = [chapter.audio_path] + [s.audio_path for s in repo.list_segments(conn, chapter_id)]
    repo.delete_chapter(conn, chapter_id)
    repo.shift_chapter_numbers(conn, chapter.book_id, chapter.number, -1)
    for relative in audio:
        if relative:
            try:
                paths.absolute(relative).unlink(missing_ok=True)
            except (paths.PathsError, OSError):
                pass


# --------------------------------------------------------------------------
# Обложка
# --------------------------------------------------------------------------

MAX_COVER_BYTES = 15 * 1024 * 1024


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _drop_cover_file(book: Book) -> None:
    if book.cover_path:
        try:
            paths.absolute(book.cover_path).unlink(missing_ok=True)
        except (paths.PathsError, OSError):
            pass


def set_cover(conn: Conn, book_id: int, data: bytes) -> Book:
    """Поставить свою картинку обложкой. Прежняя обложка удаляется."""
    book = repo.get_book(conn, book_id)
    if not data:
        raise LibraryError("пустой файл")
    if len(data) > MAX_COVER_BYTES:
        raise LibraryError("картинка больше 15 МБ — уменьшите её")
    kind = _image_type(data)
    if kind is None:
        raise LibraryError("обложкой может быть картинка JPG, PNG, WebP или GIF")
    paths.ensure_layout()
    target = _unique_name(paths.books_dir(), f"book-{book_id}.cover.{kind}")
    target.write_bytes(data)
    try:
        updated = repo.update_book(conn, book_id, cover_path=paths.relative(target))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    _drop_cover_file(book)
    return updated


def remove_cover(conn: Conn, book_id: int) -> Book:
    """Убрать картинку — обложка снова рисуется по жанру и настроению."""
    book = repo.get_book(conn, book_id)
    updated = repo.update_book(conn, book_id, cover_path=None)
    _drop_cover_file(book)
    return updated


# --------------------------------------------------------------------------
# Поиск
# --------------------------------------------------------------------------

_SNIPPET_RADIUS = 90


def _highlight_pattern(text: str) -> re.Pattern[str] | None:
    """Слова запроса как префиксы, «е» и «ё» — одна буква."""
    words = {w.lower().replace("ё", "е") for w in re.findall(r"\w+", text or "")}
    if not words:
        return None
    alternatives = sorted((re.escape(w).replace("е", "[её]") for w in words), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")\w*", re.IGNORECASE)


def _mark(text: str, pattern: re.Pattern[str] | None) -> str:
    if not pattern or not text:
        return text or ""
    return pattern.sub(lambda m: f"{repo.MARK_OPEN}{m.group(0)}{repo.MARK_CLOSE}", text)


def _snippet(text: str, pattern: re.Pattern[str] | None) -> str:
    """Фрагмент исходного текста вокруг первого совпадения.

    Строится из текста главы, а не из индекса: в индексе «ё» заменена на «е»,
    и сниппет из него исказил бы цитату.
    """
    match = pattern.search(text) if pattern else None
    if match is None:
        return ""
    start = max(0, match.start() - _SNIPPET_RADIUS)
    end = min(len(text), match.end() + _SNIPPET_RADIUS)
    if start > 0:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < match.start() else start
    if end < len(text):
        space = text.rfind(" ", match.end(), end)
        end = space if space > match.end() else end
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + _mark(piece, pattern) + ("…" if end < len(text) else "")


def search_library(conn: Conn, text: str, limit: int = 40) -> list[dict[str, Any]]:
    """Поиск по названиям папок и книг и по тексту глав.

    В названиях и сниппетах совпадения обрамлены ``\\x02``/``\\x03``.
    """
    pattern = _highlight_pattern(text)
    results: list[dict[str, Any]] = []
    for hit in repo.search(conn, text, limit=limit):
        kind, ref_id = hit["kind"], hit["ref_id"]
        try:
            if kind == "folder":
                folder = repo.get_folder(conn, ref_id)
                results.append({
                    "kind": kind, "id": folder.id, "parent_id": folder.parent_id,
                    "title": _mark(folder.name, pattern),
                })
            elif kind == "book":
                book = repo.get_book(conn, ref_id)
                results.append({
                    "kind": kind, "id": book.id, "folder_id": book.folder_id,
                    "title": _mark(book.title, pattern), "author": _mark(book.author, pattern),
                    "cover_path": book.cover_path, "genres": book.genres, "moods": book.moods,
                })
            else:
                chapter = repo.get_chapter(conn, ref_id)
                book = repo.get_book(conn, chapter.book_id)
                results.append({
                    "kind": "chapter", "id": chapter.id, "book_id": book.id,
                    "book_title": book.title, "cover_path": book.cover_path,
                    "genres": book.genres, "moods": book.moods,
                    "title": _mark(chapter.label, pattern),
                    "snippet": _snippet(chapter.text, pattern),
                })
        except repo.RepoError:  # индекс обновляется триггерами, но на всякий случай
            continue
    return results


# --------------------------------------------------------------------------
# Разметка
# --------------------------------------------------------------------------


def _manual_paragraphs(chapter: Chapter, segments: Sequence[Segment]) -> set[int]:
    """Абзацы, где есть ручные правки: их авторазметка не трогает."""
    spans = paragraph_spans(chapter.text)
    manual: set[int] = set()
    for segment in segments:
        if not segment.is_manual or segment.char_start is None:
            continue
        for index, (start, end) in enumerate(spans):
            if start <= segment.char_start < end:
                manual.add(index)
                break
    return manual


MARKUP_MODEL_SETTING = "anthropic.model"
MARKUP_URL_SETTING = "anthropic.base_url"


def markup_connection(
    conn: Conn,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Ключ, адрес и модель для разметки.

    Ключ — из системного хранилища (или окружения), адрес и модель — из
    настроек библиотеки. Без этого ключ, сохранённый на экране настроек, до
    клиента не доходил бы: SDK сам смотрит только в переменные окружения.
    """
    return {
        "api_key": api_key or secrets.get_key("anthropic") or None,
        "base_url": base_url or repo.get_setting(conn, MARKUP_URL_SETTING, "") or None,
        "model": model or repo.get_setting(conn, MARKUP_MODEL_SETTING, "") or DEFAULT_MODEL,
    }


def markup_chapter(
    conn: Conn,
    chapter_id: int,
    *,
    client: Any = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    batch_chars: int = DEFAULT_BATCH_CHARS,
    force: bool = False,
    on_progress: Any = None,
) -> MarkupResult:
    """Разметить главу и сохранить сегменты.

    Ручные правки сохраняются: абзацы с ``is_manual`` не переразмечаются, их
    сегменты остаются на своих местах. ``force`` стирает и их тоже.
    """
    connection = markup_connection(conn, api_key, base_url, model)
    chapter = repo.get_chapter(conn, chapter_id)
    book = repo.get_book(conn, chapter.book_id)
    existing = repo.list_segments(conn, chapter_id)

    keep_paragraphs = set() if force else _manual_paragraphs(chapter, existing)
    spans = paragraph_spans(chapter.text)
    kept = [
        segment
        for segment in existing
        if segment.char_start is not None
        and any(
            spans[i][0] <= segment.char_start < spans[i][1] for i in keep_paragraphs
        )
    ]

    result = markup_text(
        chapter.text,
        client=client,
        model=connection["model"],
        api_key=connection["api_key"],
        base_url=connection["base_url"],
        batch_chars=batch_chars,
        known=list(repo.book_speakers(conn, book.id)),
        book_title=book.title,
        chapter_label=chapter.label,
        skip_paragraphs=sorted(keep_paragraphs),
        on_progress=on_progress,
    )

    merged = sorted(
        kept + result.segments,
        key=lambda s: s.char_start if s.char_start is not None else 1 << 30,
    )
    repo.replace_segments(conn, chapter_id, merged)
    result.segments = repo.list_segments(conn, chapter_id)
    return result


def chapter_view(conn: Conn, chapter_id: int) -> dict[str, Any]:
    """Всё, что нужно редактору для одной главы, одним ответом."""
    chapter = repo.get_chapter(conn, chapter_id)
    segments = repo.list_segments(conn, chapter_id)
    speakers = repo.chapter_speakers(conn, chapter_id)
    cast = repo.cast_map(conn, chapter.book_id)
    slots = repo.role_slots(conn, chapter.book_id)
    return {
        "chapter": chapter.to_dict(),
        "book": repo.get_book(conn, chapter.book_id).to_dict(),
        "segments": [{**s.to_dict(), "slot": slots.get(s.speaker, 0)} for s in segments],
        "speakers": [
            {
                "name": name,
                "count": count,
                "slot": slots.get(name, 0),
                "voice": cast[name].to_dict() if name in cast else None,
            }
            for name, count in speakers.items()
        ],
        "chapters": [c.to_dict(with_text=False) for c in repo.list_chapters(conn, chapter.book_id)],
    }
