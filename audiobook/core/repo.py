"""Чтение и запись сущностей. Единственный модуль, который знает SQL.

Все функции принимают открытое соединение первым аргументом: транзакцией
управляет вызывающий, поэтому несколько операций складываются в одну.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable, Sequence

from .models import Book, CastEntry, Chapter, Folder, Job, JobStatus, Segment, Voice

__all__ = [
    "RepoError", "InvalidOperation",
    "create_folder", "get_folder", "list_folders", "folder_tree", "rename_folder",
    "move_folder", "delete_folder", "folder_subtree_ids",
    "create_book", "get_book", "list_books", "delete_book", "update_book", "move_book",
    "create_chapter", "get_chapter", "get_chapter_by_number", "list_chapters",
    "update_chapter", "delete_chapter", "chapter_stats", "next_chapter",
    "shift_chapter_numbers", "move_segments", "reset_chapter_derivatives", "book_summaries",
    "get_setting", "set_setting", "all_settings",
    "fts_query", "search",
    "get_playback", "set_playback", "recent_playback",
    "get_queue", "set_queue",
    "list_pronunciations", "set_pronunciation", "delete_pronunciation",
    "list_profiles", "get_profile", "save_profile", "delete_profile",
    "record_usage", "usage_summary", "usage_by_book", "audio_totals", "audio_by_book",
    "replace_segments", "list_segments", "get_segment", "insert_segment",
    "update_segment", "delete_segment", "renumber_segments",
    "chapter_speakers", "book_speakers",
    "upsert_voice", "list_voices", "get_voice",
    "set_cast", "get_cast", "cast_map",
    "create_job", "update_job", "update_job_fields", "finish_job", "get_job", "list_jobs",
    "unnotified_jobs", "clear_finished_jobs",
]


class RepoError(RuntimeError):
    """Запрошенной сущности нет."""


class InvalidOperation(RepoError):
    """Сущность есть, но так с ней нельзя: пустое имя, цикл в дереве папок."""


Conn = sqlite3.Connection


# --------------------------------------------------------------------------
# Папки
# --------------------------------------------------------------------------


def _next_position(conn: Conn, table: str, parent_column: str, parent_id: int | None) -> int:
    """Позиция в конце списка соседей — новое встаёт последним."""
    if parent_id is None:
        row = conn.execute(
            f"SELECT COALESCE(MAX(position), -1) + 1 FROM {table} WHERE {parent_column} IS NULL"
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT COALESCE(MAX(position), -1) + 1 FROM {table} WHERE {parent_column} = ?",
            (parent_id,),
        ).fetchone()
    return int(row[0])


def create_folder(conn: Conn, name: str, parent_id: int | None = None) -> Folder:
    name = (name or "").strip()
    if not name:
        raise InvalidOperation("у папки должно быть имя")
    if parent_id is not None:
        get_folder(conn, parent_id)
    cursor = conn.execute(
        "INSERT INTO folder (parent_id, name, position) VALUES (?, ?, ?)",
        (parent_id, name, _next_position(conn, "folder", "parent_id", parent_id)),
    )
    return get_folder(conn, cursor.lastrowid)


def rename_folder(conn: Conn, folder_id: int, name: str) -> Folder:
    name = (name or "").strip()
    if not name:
        raise InvalidOperation("у папки должно быть имя")
    get_folder(conn, folder_id)
    conn.execute("UPDATE folder SET name = ? WHERE id = ?", (name, folder_id))
    return get_folder(conn, folder_id)


def folder_subtree_ids(conn: Conn, folder_id: int) -> list[int]:
    """Папка и все вложенные в неё, на любую глубину."""
    rows = conn.execute(
        "WITH RECURSIVE sub(id) AS ("
        "  SELECT ? UNION ALL SELECT f.id FROM folder f JOIN sub ON f.parent_id = sub.id"
        ") SELECT id FROM sub",
        (folder_id,),
    ).fetchall()
    return [row[0] for row in rows]


def move_folder(
    conn: Conn, folder_id: int, parent_id: int | None, position: int | None = None
) -> Folder:
    """Перенести папку под другого родителя (``None`` — в корень).

    Папку нельзя положить в саму себя или в собственную вложенную: дерево
    превратилось бы в цикл и пропало из навигации.
    """
    get_folder(conn, folder_id)
    if parent_id is not None:
        get_folder(conn, parent_id)
        if parent_id in folder_subtree_ids(conn, folder_id):
            raise InvalidOperation("папку нельзя переместить внутрь неё самой")
    _place(conn, "folder", "parent_id", folder_id, parent_id, position)
    return get_folder(conn, folder_id)


def delete_folder(conn: Conn, folder_id: int) -> None:
    """Удалить папку со вложенными. Книги не удаляются — уходят к родителю."""
    folder = get_folder(conn, folder_id)
    subtree = folder_subtree_ids(conn, folder_id)
    marks = ", ".join("?" for _ in subtree)
    start = _next_position(conn, "book", "folder_id", folder.parent_id)
    rows = conn.execute(
        f"SELECT id FROM book WHERE folder_id IN ({marks}) ORDER BY position, title", subtree
    ).fetchall()
    conn.executemany(
        "UPDATE book SET folder_id = ?, position = ? WHERE id = ?",
        [(folder.parent_id, start + i, row["id"]) for i, row in enumerate(rows)],
    )
    conn.execute("DELETE FROM folder WHERE id = ?", (folder_id,))


def _place(
    conn: Conn,
    table: str,
    parent_column: str,
    item_id: int,
    parent_id: int | None,
    position: int | None,
) -> None:
    """Поставить элемент к новому родителю на нужное место среди соседей."""
    order_by = "position, name" if table == "folder" else "position, title"
    if parent_id is None:
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE {parent_column} IS NULL AND id != ? ORDER BY {order_by}",
            (item_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE {parent_column} = ? AND id != ? ORDER BY {order_by}",
            (parent_id, item_id),
        ).fetchall()
    siblings = [row["id"] for row in rows]
    index = len(siblings) if position is None else max(0, min(position, len(siblings)))
    siblings.insert(index, item_id)
    conn.execute(f"UPDATE {table} SET {parent_column} = ? WHERE id = ?", (parent_id, item_id))
    conn.executemany(
        f"UPDATE {table} SET position = ? WHERE id = ?",
        [(i, sibling) for i, sibling in enumerate(siblings)],
    )


def get_folder(conn: Conn, folder_id: int) -> Folder:
    row = conn.execute("SELECT * FROM folder WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise RepoError(f"папки {folder_id} нет")
    return Folder.from_row(row)


def list_folders(conn: Conn, parent_id: int | None = None) -> list[Folder]:
    if parent_id is None:
        rows = conn.execute(
            "SELECT * FROM folder WHERE parent_id IS NULL ORDER BY position, name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM folder WHERE parent_id = ? ORDER BY position, name", (parent_id,)
        ).fetchall()
    return [Folder.from_row(r) for r in rows]


def folder_tree(conn: Conn, parent_id: int | None = None) -> list[dict[str, Any]]:
    """Дерево папок с книгами — для навигации по библиотеке."""
    out: list[dict[str, Any]] = []
    for folder in list_folders(conn, parent_id):
        out.append(
            {
                **folder.to_dict(),
                "books": [b.to_dict() for b in list_books(conn, folder.id)],
                "folders": folder_tree(conn, folder.id),
            }
        )
    return out


# --------------------------------------------------------------------------
# Книги
# --------------------------------------------------------------------------


def create_book(
    conn: Conn,
    title: str,
    author: str = "",
    source_path: str = "",
    folder_id: int | None = None,
    cover_path: str | None = None,
) -> Book:
    if folder_id is not None:
        get_folder(conn, folder_id)
    cursor = conn.execute(
        "INSERT INTO book (folder_id, title, author, source_path, cover_path, position) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            folder_id, title, author, source_path, cover_path,
            _next_position(conn, "book", "folder_id", folder_id),
        ),
    )
    return get_book(conn, cursor.lastrowid)


def move_book(
    conn: Conn, book_id: int, folder_id: int | None, position: int | None = None
) -> Book:
    get_book(conn, book_id)
    if folder_id is not None:
        get_folder(conn, folder_id)
    _place(conn, "book", "folder_id", book_id, folder_id, position)
    return get_book(conn, book_id)


def get_book(conn: Conn, book_id: int) -> Book:
    row = conn.execute("SELECT * FROM book WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise RepoError(f"книги {book_id} нет")
    return Book.from_row(row)


def list_books(conn: Conn, folder_id: int | None = ...) -> list[Book]:
    """Книги папки; без аргумента — все книги библиотеки."""
    if folder_id is ...:
        rows = conn.execute("SELECT * FROM book ORDER BY title").fetchall()
    elif folder_id is None:
        rows = conn.execute(
            "SELECT * FROM book WHERE folder_id IS NULL ORDER BY position, title"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM book WHERE folder_id = ? ORDER BY position, title", (folder_id,)
        ).fetchall()
    return [Book.from_row(r) for r in rows]


def update_book(conn: Conn, book_id: int, **fields) -> Book:
    allowed = {"title", "author", "source_path", "cover_path", "language"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "title" in updates and not str(updates["title"]).strip():
        raise InvalidOperation("у книги должно быть название")
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE book SET {assignments} WHERE id = ?",
            (*updates.values(), book_id),
        )
    return get_book(conn, book_id)


def delete_book(conn: Conn, book_id: int) -> None:
    conn.execute("DELETE FROM book WHERE id = ?", (book_id,))


# --------------------------------------------------------------------------
# Главы
# --------------------------------------------------------------------------


def create_chapter(
    conn: Conn, book_id: int, number: int, title: str = "", text: str = ""
) -> Chapter:
    cursor = conn.execute(
        "INSERT INTO chapter (book_id, number, title, text) VALUES (?, ?, ?, ?)",
        (book_id, number, title, text),
    )
    return get_chapter(conn, cursor.lastrowid)


def get_chapter(conn: Conn, chapter_id: int) -> Chapter:
    row = conn.execute("SELECT * FROM chapter WHERE id = ?", (chapter_id,)).fetchone()
    if row is None:
        raise RepoError(f"главы {chapter_id} нет")
    return Chapter.from_row(row)


def get_chapter_by_number(conn: Conn, book_id: int, number: int) -> Chapter:
    row = conn.execute(
        "SELECT * FROM chapter WHERE book_id = ? AND number = ?", (book_id, number)
    ).fetchone()
    if row is None:
        raise RepoError(f"в книге {book_id} нет главы {number}")
    return Chapter.from_row(row)


def list_chapters(conn: Conn, book_id: int) -> list[Chapter]:
    rows = conn.execute(
        "SELECT * FROM chapter WHERE book_id = ? ORDER BY number", (book_id,)
    ).fetchall()
    return [Chapter.from_row(r) for r in rows]


def update_chapter(conn: Conn, chapter_id: int, **fields) -> Chapter:
    allowed = {"title", "text", "number", "audio_path", "audio_hash", "duration_ms"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    get_chapter(conn, chapter_id)
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE chapter SET {assignments} WHERE id = ?", (*updates.values(), chapter_id)
        )
    return get_chapter(conn, chapter_id)


def delete_chapter(conn: Conn, chapter_id: int) -> None:
    conn.execute("DELETE FROM chapter WHERE id = ?", (chapter_id,))


def next_chapter(conn: Conn, chapter: Chapter) -> Chapter | None:
    row = conn.execute(
        "SELECT * FROM chapter WHERE book_id = ? AND number > ? ORDER BY number LIMIT 1",
        (chapter.book_id, chapter.number),
    ).fetchone()
    return Chapter.from_row(row) if row else None


def shift_chapter_numbers(conn: Conn, book_id: int, after_number: int, delta: int) -> None:
    """Сдвинуть номера глав после ``after_number`` на ``delta``.

    В два шага через отрицательные числа: построчный UPDATE иначе упирается
    в UNIQUE (book_id, number) на полпути.
    """
    conn.execute(
        "UPDATE chapter SET number = -(number + ?) WHERE book_id = ? AND number > ?",
        (delta, book_id, after_number),
    )
    conn.execute("UPDATE chapter SET number = -number WHERE book_id = ? AND number < 0", (book_id,))


def move_segments(
    conn: Conn, from_chapter: int, to_chapter: int, min_start: int, offset_delta: int
) -> None:
    """Перенести сегменты, начинающиеся с ``min_start``, в другую главу со сдвигом смещений.

    Порядок уводится далеко вперёд, чтобы после переноса встать в конец;
    :func:`renumber_segments` затем выравнивает его.
    """
    conn.execute(
        'UPDATE segment SET chapter_id = ?, char_start = char_start + ?, '
        'char_end = char_end + ?, "order" = "order" + 1000000 '
        "WHERE chapter_id = ? AND char_start IS NOT NULL AND char_start >= ?",
        (to_chapter, offset_delta, offset_delta, from_chapter, min_start),
    )


def reset_chapter_derivatives(conn: Conn, chapter_ids: Sequence[int]) -> None:
    """Текст главы сдвинулся: склейка, история правок и позиция чтения устарели.

    Озвучка отдельных реплик остаётся — её хеш зависит от текста реплики, а не
    от места в главе.
    """
    if not chapter_ids:
        return
    marks = ", ".join("?" for _ in chapter_ids)
    conn.execute(
        f"UPDATE chapter SET audio_path = NULL, audio_hash = NULL, duration_ms = NULL "
        f"WHERE id IN ({marks})",
        list(chapter_ids),
    )
    conn.execute(
        f"UPDATE segment SET audio_start_ms = NULL, audio_end_ms = NULL WHERE chapter_id IN ({marks})",
        list(chapter_ids),
    )
    # Снимки undo хранят смещения старого текста — применять их больше нельзя.
    conn.execute(f"DELETE FROM edit_op WHERE chapter_id IN ({marks})", list(chapter_ids))
    conn.execute(
        f"UPDATE playback SET position_ms = 0 WHERE chapter_id IN ({marks})", list(chapter_ids)
    )


def book_summaries(conn: Conn) -> dict[int, dict[str, Any]]:
    """Все книги с числом глав и длительностью — для дерева библиотеки."""
    rows = conn.execute(
        "SELECT b.*, COUNT(c.id) AS n_chapters, COALESCE(SUM(c.duration_ms), 0) AS duration_ms, "
        "SUM(CASE WHEN c.audio_path IS NOT NULL THEN 1 ELSE 0 END) AS voiced_chapters "
        "FROM book b LEFT JOIN chapter c ON c.book_id = b.id GROUP BY b.id"
    ).fetchall()
    return {
        row["id"]: {
            **Book.from_row(row).to_dict(),
            "chapters": row["n_chapters"],
            "duration_ms": row["duration_ms"] or 0,
            "voiced_chapters": row["voiced_chapters"] or 0,
        }
        for row in rows
    }


def chapter_stats(conn: Conn, book_id: int) -> dict[int, dict[str, int]]:
    """По главам книги: сегменты, озвученные, с ошибкой — одним запросом."""
    rows = conn.execute(
        "SELECT c.id AS id, COUNT(s.id) AS segments, "
        "SUM(CASE WHEN s.audio_path IS NOT NULL THEN 1 ELSE 0 END) AS voiced, "
        "SUM(CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
        "FROM chapter c LEFT JOIN segment s ON s.chapter_id = c.id "
        "WHERE c.book_id = ? GROUP BY c.id",
        (book_id,),
    ).fetchall()
    return {
        row["id"]: {
            "segments": row["segments"] or 0,
            "voiced": row["voiced"] or 0,
            "errors": row["errors"] or 0,
        }
        for row in rows
    }


# --------------------------------------------------------------------------
# Сегменты
# --------------------------------------------------------------------------

_SEGMENT_FIELDS = (
    "chapter_id", '"order"', "speaker", "text", "emotion",
    "audio_path", "audio_hash", "is_manual", "char_start", "char_end", "error",
)


def _segment_values(segment: Segment, chapter_id: int, order: int) -> tuple:
    return (
        chapter_id,
        order,
        segment.speaker,
        segment.text,
        segment.emotion,
        segment.audio_path,
        segment.audio_hash,
        int(bool(segment.is_manual)),
        segment.char_start,
        segment.char_end,
        segment.error,
    )


def insert_segment(conn: Conn, chapter_id: int, segment: Segment, order: int) -> Segment:
    placeholders = ", ".join("?" for _ in _SEGMENT_FIELDS)
    cursor = conn.execute(
        f"INSERT INTO segment ({', '.join(_SEGMENT_FIELDS)}) VALUES ({placeholders})",
        _segment_values(segment, chapter_id, order),
    )
    return get_segment(conn, cursor.lastrowid)


def replace_segments(
    conn: Conn, chapter_id: int, segments: Sequence[Segment]
) -> list[Segment]:
    """Заменить разметку главы целиком. Порядок задаётся позицией в списке."""
    conn.execute("DELETE FROM segment WHERE chapter_id = ?", (chapter_id,))
    placeholders = ", ".join("?" for _ in _SEGMENT_FIELDS)
    conn.executemany(
        f"INSERT INTO segment ({', '.join(_SEGMENT_FIELDS)}) VALUES ({placeholders})",
        [_segment_values(s, chapter_id, i) for i, s in enumerate(segments)],
    )
    return list_segments(conn, chapter_id)


def list_segments(conn: Conn, chapter_id: int) -> list[Segment]:
    rows = conn.execute(
        'SELECT * FROM segment WHERE chapter_id = ? ORDER BY "order", id', (chapter_id,)
    ).fetchall()
    return [Segment.from_row(r) for r in rows]


def get_segment(conn: Conn, segment_id: int) -> Segment:
    row = conn.execute("SELECT * FROM segment WHERE id = ?", (segment_id,)).fetchone()
    if row is None:
        raise RepoError(f"сегмента {segment_id} нет")
    return Segment.from_row(row)


def update_segment(conn: Conn, segment_id: int, **fields) -> Segment:
    allowed = {
        "speaker", "text", "emotion", "audio_path", "audio_hash",
        "is_manual", "char_start", "char_end", "error", "order",
        "audio_start_ms", "audio_end_ms",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_segment(conn, segment_id)
    if "is_manual" in updates:
        updates["is_manual"] = int(bool(updates["is_manual"]))
    assignments = ", ".join(
        f'"order" = ?' if k == "order" else f"{k} = ?" for k in updates
    )
    conn.execute(
        f"UPDATE segment SET {assignments} WHERE id = ?",
        (*updates.values(), segment_id),
    )
    return get_segment(conn, segment_id)


def delete_segment(conn: Conn, segment_id: int) -> None:
    conn.execute("DELETE FROM segment WHERE id = ?", (segment_id,))


def renumber_segments(conn: Conn, chapter_id: int) -> None:
    """Выровнять ``order`` в 0..N-1 после вставок и удалений."""
    rows = conn.execute(
        'SELECT id FROM segment WHERE chapter_id = ? ORDER BY "order", id', (chapter_id,)
    ).fetchall()
    conn.executemany(
        'UPDATE segment SET "order" = ? WHERE id = ?',
        [(i, row["id"]) for i, row in enumerate(rows)],
    )


def chapter_speakers(conn: Conn, chapter_id: int) -> dict[str, int]:
    """Персонажи главы и число реплик, по убыванию — для горячих клавиш 1–9."""
    rows = conn.execute(
        "SELECT speaker, COUNT(*) AS n FROM segment WHERE chapter_id = ? "
        "GROUP BY speaker ORDER BY n DESC, speaker",
        (chapter_id,),
    ).fetchall()
    return {row["speaker"]: row["n"] for row in rows}


def book_speakers(conn: Conn, book_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT s.speaker AS speaker, COUNT(*) AS n FROM segment s "
        "JOIN chapter c ON c.id = s.chapter_id WHERE c.book_id = ? "
        "GROUP BY s.speaker ORDER BY n DESC, s.speaker",
        (book_id,),
    ).fetchall()
    return {row["speaker"]: row["n"] for row in rows}


# --------------------------------------------------------------------------
# Голоса и распределение ролей
# --------------------------------------------------------------------------


def upsert_voice(conn: Conn, voice: Voice) -> Voice:
    """Добавить или обновить голос каталога (движок + ключ уникальны)."""
    conn.execute(
        "INSERT INTO voice (engine, voice_key, display_name, gender, language, preview_path, "
        "tags, available, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT (engine, voice_key) DO UPDATE SET "
        "display_name = excluded.display_name, gender = excluded.gender, "
        "language = excluded.language, tags = excluded.tags, "
        "available = excluded.available, updated_at = excluded.updated_at, "
        "preview_path = COALESCE(excluded.preview_path, voice.preview_path)",
        (
            voice.engine, voice.voice_key, voice.display_name,
            voice.gender, voice.language, voice.preview_path,
            voice.tags, int(bool(voice.available)),
        ),
    )
    row = conn.execute(
        "SELECT * FROM voice WHERE engine = ? AND voice_key = ?",
        (voice.engine, voice.voice_key),
    ).fetchone()
    return Voice.from_row(row)


def get_voice(conn: Conn, voice_id: int) -> Voice:
    row = conn.execute("SELECT * FROM voice WHERE id = ?", (voice_id,)).fetchone()
    if row is None:
        raise RepoError(f"голоса {voice_id} нет")
    return Voice.from_row(row)


def list_voices(
    conn: Conn,
    engine: str | None = None,
    gender: str | None = None,
    language: str | None = None,
    available_only: bool = False,
    search: str | None = None,
) -> list[Voice]:
    where, params = [], []
    for column, value in (("engine", engine), ("gender", gender), ("language", language)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if available_only:
        where.append("available = 1")
    if search:
        where.append("(display_name LIKE ? OR voice_key LIKE ? OR tags LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM voice {clause} ORDER BY engine, display_name, voice_key", params
    ).fetchall()
    return [Voice.from_row(r) for r in rows]


def mark_missing_voices(conn: Conn, engine: str, present_keys: Sequence[str]) -> int:
    """Пометить недоступными голоса движка, которых больше нет в его каталоге.

    Строки не удаляем: на голос может ссылаться уже назначенная роль, и связь
    с ней важнее, чем чистота списка.
    """
    marks = ", ".join("?" for _ in present_keys)
    clause = f" AND voice_key NOT IN ({marks})" if present_keys else ""
    cursor = conn.execute(
        f"UPDATE voice SET available = 0 WHERE engine = ? AND available = 1{clause}",
        [engine, *present_keys],
    )
    return cursor.rowcount or 0


def set_voice_preview(conn: Conn, voice_id: int, preview_path: str) -> Voice:
    conn.execute("UPDATE voice SET preview_path = ? WHERE id = ?", (preview_path, voice_id))
    return get_voice(conn, voice_id)


def set_cast(
    conn: Conn,
    book_id: int,
    speaker: str,
    voice_id: int | None,
    rate: float = 1.0,
    pitch: float = 1.0,
    volume: float = 1.0,
) -> CastEntry:
    conn.execute(
        'INSERT INTO "cast" (book_id, speaker, voice_id, rate, pitch, volume) '
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (book_id, speaker) DO UPDATE SET "
        "voice_id = excluded.voice_id, rate = excluded.rate, "
        "pitch = excluded.pitch, volume = excluded.volume",
        (book_id, speaker, voice_id, rate, pitch, volume),
    )
    return get_cast(conn, book_id, speaker)


def get_cast(conn: Conn, book_id: int, speaker: str) -> CastEntry:
    row = conn.execute(
        'SELECT * FROM "cast" WHERE book_id = ? AND speaker = ?', (book_id, speaker)
    ).fetchone()
    if row is None:
        raise RepoError(f"для «{speaker}» голос не назначен")
    return _cast_from_row(conn, row)


def cast_map(conn: Conn, book_id: int) -> dict[str, CastEntry]:
    rows = conn.execute(
        'SELECT * FROM "cast" WHERE book_id = ?', (book_id,)
    ).fetchall()
    return {row["speaker"]: _cast_from_row(conn, row) for row in rows}


def _cast_from_row(conn: Conn, row) -> CastEntry:
    voice = None
    if row["voice_id"] is not None:
        try:
            voice = get_voice(conn, row["voice_id"])
        except RepoError:  # голос удалили из каталога — роль осталась без него
            voice = None
    return CastEntry(
        id=row["id"],
        book_id=row["book_id"],
        speaker=row["speaker"],
        voice_id=row["voice_id"],
        rate=row["rate"],
        pitch=row["pitch"],
        volume=row["volume"],
        voice=voice,
    )


# --------------------------------------------------------------------------
# Задачи
# --------------------------------------------------------------------------


def create_job(conn: Conn, kind: str, target_id: int | None = None, total: int = 0) -> Job:
    cursor = conn.execute(
        "INSERT INTO job (kind, target_id, total, status) VALUES (?, ?, ?, ?)",
        (kind, target_id, total, JobStatus.PENDING),
    )
    return get_job(conn, cursor.lastrowid)


def get_job(conn: Conn, job_id: int) -> Job:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise RepoError(f"задачи {job_id} нет")
    return Job.from_row(row)


def update_job(conn: Conn, job_id: int, **fields) -> Job:
    allowed = {"status", "progress", "total", "done", "error"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "done" in updates and "progress" not in updates:
        # total из этого же вызова важнее сохранённого: иначе доля считается
        # по старому знаменателю и скачет.
        total = updates.get("total")
        if total is None:
            row = conn.execute("SELECT total FROM job WHERE id = ?", (job_id,)).fetchone()
            total = row["total"] if row else 0
        if total:
            updates["progress"] = min(1.0, updates["done"] / total)
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE job SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            (*updates.values(), job_id),
        )
    return get_job(conn, job_id)


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


def get_setting(conn: Conn, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_setting(conn: Conn, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def all_settings(conn: Conn) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM setting").fetchall():
        try:
            out[row["key"]] = json.loads(row["value"])
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# Поиск
# --------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)
# Маркеры подсветки в сниппетах. Управляющие символы не встречаются в тексте
# книги, и интерфейс превращает их в <mark> уже после экранирования HTML.
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"


def fts_query(text: str) -> str:
    """Запрос пользователя -> выражение FTS5: все слова, каждое как префикс.

    Кавычки вокруг слова не дают FTS5 принять его за оператор (``NOT``,
    ``NEAR``), а ``\\w+`` не пропускает внутрь самих кавычек.
    """
    words = [w.replace("ё", "е").replace("Ё", "Е") for w in _WORD.findall(text or "")]
    return " ".join(f'"{word}"*' for word in words)


def search(conn: Conn, text: str, limit: int = 50) -> list[dict[str, Any]]:
    """Папки, книги и главы, где встречаются все слова запроса.

    Совпадение в названии весит больше, чем в тексте главы.
    """
    query = fts_query(text)
    if not query:
        return []
    rows = conn.execute(
        "SELECT kind, ref_id, "
        "highlight(search_index, 2, char(2), char(3)) AS title, "
        "snippet(search_index, 3, char(2), char(3), '…', 16) AS snippet, "
        "bm25(search_index, 0.0, 0.0, 8.0, 1.0) AS score "
        "FROM search_index WHERE search_index MATCH ? ORDER BY score LIMIT ?",
        (query, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Воспроизведение: позиции и очередь
# --------------------------------------------------------------------------


def get_playback(conn: Conn, book_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM playback WHERE book_id = ?", (book_id,)).fetchone()
    return dict(row) if row else None


def set_playback(conn: Conn, book_id: int, chapter_id: int | None, position_ms: int) -> None:
    conn.execute(
        "INSERT INTO playback (book_id, chapter_id, position_ms, updated_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT (book_id) DO UPDATE SET chapter_id = excluded.chapter_id, "
        "position_ms = excluded.position_ms, updated_at = excluded.updated_at",
        (book_id, chapter_id, max(0, int(position_ms))),
    )


def recent_playback(conn: Conn, limit: int = 6) -> list[dict[str, Any]]:
    """Книги, которые слушали последними, с долей прослушанного.

    Доля считается по длительности: главы до текущей плюс позиция в ней,
    делённые на всё озвученное в книге.
    """
    rows = conn.execute(
        "SELECT p.book_id, p.chapter_id, p.position_ms, p.updated_at, "
        "b.title, b.author, b.cover_path, c.number, c.title AS chapter_title, "
        "c.duration_ms AS chapter_ms "
        "FROM playback p JOIN book b ON b.id = p.book_id "
        "LEFT JOIN chapter c ON c.id = p.chapter_id "
        "ORDER BY p.updated_at DESC, p.book_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        totals = conn.execute(
            "SELECT COALESCE(SUM(duration_ms), 0) AS total, "
            "COALESCE(SUM(CASE WHEN number < ? THEN duration_ms ELSE 0 END), 0) AS before "
            "FROM chapter WHERE book_id = ? AND audio_path IS NOT NULL",
            (row["number"] or 0, row["book_id"]),
        ).fetchone()
        total = totals["total"] or 0
        listened = (totals["before"] or 0) + (row["position_ms"] or 0)
        label = f"Глава {row['number']}" + (f". {row['chapter_title']}" if row["chapter_title"] else "")
        out.append({
            "book_id": row["book_id"], "chapter_id": row["chapter_id"],
            "title": row["title"], "author": row["author"], "cover_path": row["cover_path"],
            "chapter_label": label if row["number"] is not None else "",
            "position_ms": row["position_ms"] or 0, "chapter_ms": row["chapter_ms"] or 0,
            "total_ms": total, "listened_ms": min(listened, total),
            "progress": min(1.0, listened / total) if total else 0.0,
            "updated_at": row["updated_at"],
        })
    return out


def get_queue(conn: Conn) -> list[int]:
    rows = conn.execute("SELECT chapter_id FROM play_queue ORDER BY position").fetchall()
    return [row["chapter_id"] for row in rows]


def set_queue(conn: Conn, chapter_ids: Sequence[int]) -> list[int]:
    conn.execute("DELETE FROM play_queue")
    conn.executemany(
        "INSERT INTO play_queue (position, chapter_id) VALUES (?, ?)",
        [(i, chapter_id) for i, chapter_id in enumerate(chapter_ids)],
    )
    return get_queue(conn)


# --------------------------------------------------------------------------
# Словарь произношений
# --------------------------------------------------------------------------


def list_pronunciations(conn: Conn, book_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pronunciation WHERE book_id = ? ORDER BY term COLLATE NOCASE", (book_id,)
    ).fetchall()
    return [
        {**dict(row), "whole_word": bool(row["whole_word"]),
         "case_sensitive": bool(row["case_sensitive"])}
        for row in rows
    ]


def set_pronunciation(
    conn: Conn,
    book_id: int,
    term: str,
    replacement: str,
    whole_word: bool = True,
    case_sensitive: bool = False,
) -> None:
    term = (term or "").strip()
    if not term:
        raise InvalidOperation("пустое слово в словаре произношений")
    get_book(conn, book_id)
    conn.execute(
        "INSERT INTO pronunciation (book_id, term, replacement, whole_word, case_sensitive) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (book_id, term) DO UPDATE SET "
        "replacement = excluded.replacement, whole_word = excluded.whole_word, "
        "case_sensitive = excluded.case_sensitive",
        (book_id, term, replacement.strip(), int(whole_word), int(case_sensitive)),
    )


def delete_pronunciation(conn: Conn, pronunciation_id: int) -> None:
    conn.execute("DELETE FROM pronunciation WHERE id = ?", (pronunciation_id,))


# --------------------------------------------------------------------------
# Профили озвучки
# --------------------------------------------------------------------------


def list_profiles(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.id, p.name, p.created_at, COUNT(e.id) AS entries FROM voice_profile p "
        "LEFT JOIN voice_profile_entry e ON e.profile_id = p.id "
        "GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def get_profile(conn: Conn, profile_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM voice_profile WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise RepoError(f"профиля {profile_id} нет")
    entries = conn.execute(
        "SELECT speaker, engine, voice_key, rate, pitch, volume FROM voice_profile_entry "
        "WHERE profile_id = ? ORDER BY speaker",
        (profile_id,),
    ).fetchall()
    return {**dict(row), "entries": [dict(e) for e in entries]}


def save_profile(conn: Conn, name: str, entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Создать профиль или перезаписать одноимённый."""
    name = (name or "").strip()
    if not name:
        raise InvalidOperation("у профиля должно быть имя")
    conn.execute(
        "INSERT INTO voice_profile (name) VALUES (?) ON CONFLICT (name) DO NOTHING", (name,)
    )
    profile_id = conn.execute(
        "SELECT id FROM voice_profile WHERE name = ?", (name,)
    ).fetchone()["id"]
    conn.execute("DELETE FROM voice_profile_entry WHERE profile_id = ?", (profile_id,))
    conn.executemany(
        "INSERT INTO voice_profile_entry "
        "(profile_id, speaker, engine, voice_key, rate, pitch, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                profile_id, e["speaker"], e["engine"], e["voice_key"],
                float(e.get("rate", 1.0)), float(e.get("pitch", 1.0)), float(e.get("volume", 1.0)),
            )
            for e in entries
        ],
    )
    return get_profile(conn, profile_id)


def delete_profile(conn: Conn, profile_id: int) -> None:
    conn.execute("DELETE FROM voice_profile WHERE id = ?", (profile_id,))


# --------------------------------------------------------------------------
# Расход API
# --------------------------------------------------------------------------


def record_usage(
    conn: Conn,
    service: str,
    operation: str = "",
    chars: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    book_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO api_usage (service, operation, chars, input_tokens, output_tokens, book_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (service, operation, int(chars), int(input_tokens), int(output_tokens), book_id),
    )


def audio_totals(conn: Conn) -> dict[str, Any]:
    """Сколько всего готового аудио в библиотеке."""
    row = conn.execute(
        "SELECT COUNT(*) AS chapters, COALESCE(SUM(duration_ms), 0) AS duration_ms, "
        "COUNT(DISTINCT book_id) AS books FROM chapter WHERE audio_path IS NOT NULL"
    ).fetchone()
    segments = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN audio_path IS NOT NULL THEN 1 ELSE 0 END) AS voiced, "
        "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors FROM segment"
    ).fetchone()
    return {
        "chapters": row["chapters"], "duration_ms": row["duration_ms"], "books": row["books"],
        "segments": segments["total"] or 0, "voiced_segments": segments["voiced"] or 0,
        "errors": segments["errors"] or 0,
    }


def audio_by_book(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT b.id, b.title, COUNT(c.id) AS chapters, "
        "SUM(CASE WHEN c.audio_path IS NOT NULL THEN 1 ELSE 0 END) AS voiced, "
        "COALESCE(SUM(c.duration_ms), 0) AS duration_ms "
        "FROM book b LEFT JOIN chapter c ON c.book_id = b.id "
        "GROUP BY b.id ORDER BY duration_ms DESC, b.title"
    ).fetchall()
    return [dict(row) for row in rows]


def usage_by_book(conn: Conn, since: str | None = None) -> list[dict[str, Any]]:
    clause, params = ("WHERE u.created_at >= ?", [since]) if since else ("", [])
    rows = conn.execute(
        "SELECT u.service, COALESCE(b.title, '—') AS title, SUM(u.chars) AS chars, "
        "SUM(u.input_tokens + u.output_tokens) AS tokens "
        f"FROM api_usage u LEFT JOIN book b ON b.id = u.book_id {clause} "
        "GROUP BY u.service, u.book_id ORDER BY chars DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def usage_summary(conn: Conn, since: str | None = None) -> list[dict[str, Any]]:
    """Расход по сервисам; ``since`` — дата ``YYYY-MM-DD`` включительно."""
    clause, params = ("WHERE created_at >= ?", [since]) if since else ("", [])
    rows = conn.execute(
        "SELECT service, COUNT(*) AS calls, SUM(chars) AS chars, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens "
        f"FROM api_usage {clause} GROUP BY service ORDER BY service",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def update_job_fields(conn: Conn, job_id: int, **fields) -> Job:
    """Поля задачи, не связанные с ходом выполнения: имя, параметры, родитель."""
    allowed = {"title", "payload", "parent_id", "notified"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "notified" in updates:
        updates["notified"] = int(bool(updates["notified"]))
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE job SET {assignments} WHERE id = ?", (*updates.values(), job_id))
    return get_job(conn, job_id)


def finish_job(conn: Conn, job_id: int) -> Job:
    conn.execute(
        "UPDATE job SET finished_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (job_id,),
    )
    return get_job(conn, job_id)


def unnotified_jobs(conn: Conn) -> list[Job]:
    """Завершённые задачи, о которых пользователю ещё не сказали."""
    rows = conn.execute(
        "SELECT * FROM job WHERE notified = 0 AND status IN (?, ?) ORDER BY id",
        (JobStatus.DONE, JobStatus.FAILED),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def clear_finished_jobs(conn: Conn) -> int:
    cursor = conn.execute(
        "DELETE FROM job WHERE status IN (?, ?, ?)",
        (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED),
    )
    return cursor.rowcount or 0


def list_jobs(conn: Conn, kind: str | None = None, active_only: bool = False) -> list[Job]:
    where, params = [], []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if active_only:
        where.append("status IN (?, ?)")
        params.extend([JobStatus.PENDING, JobStatus.RUNNING])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM job {clause} ORDER BY id DESC", params
    ).fetchall()
    return [Job.from_row(r) for r in rows]
