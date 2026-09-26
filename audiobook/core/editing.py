"""Операции редактора разметки и журнал отмены.

Каждая операция записывает в ``edit_op`` снимок сегментов до и после. Отмена
— это возврат к снимку «до», повтор — к снимку «после». Журнал лежит в базе,
а не в памяти процесса: отмена переживает перезагрузку страницы, и её можно
проверить тестами без всякого UI.

Все операции, которые меняет человек, ставят сегменту ``is_manual`` —
повторная автоматическая разметка такие абзацы не трогает.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from . import repo
from .models import EMOTIONS, NARRATOR, Segment

__all__ = [
    "EditError",
    "assign_speaker",
    "assign_range",
    "split_segment",
    "merge_segments",
    "rename_speaker",
    "set_emotion",
    "set_text",
    "undo",
    "redo",
    "history_state",
    "snapshot",
]

Conn = sqlite3.Connection


class EditError(RuntimeError):
    """Операция невозможна: не те сегменты, пустой диапазон и подобное."""


# --------------------------------------------------------------------------
# Снимки и журнал
# --------------------------------------------------------------------------

_SNAPSHOT_FIELDS = (
    "speaker", "text", "emotion", "audio_path", "audio_hash",
    "is_manual", "char_start", "char_end", "error",
)


def snapshot(conn: Conn, chapter_id: int) -> str:
    """Сегменты главы в виде JSON — единица отмены."""
    segments = repo.list_segments(conn, chapter_id)
    return json.dumps(
        [{f: getattr(s, f) for f in _SNAPSHOT_FIELDS} for s in segments],
        ensure_ascii=False,
    )


def _restore(conn: Conn, chapter_id: int, blob: str) -> list[Segment]:
    data = json.loads(blob)
    segments = [Segment(**item) for item in data]
    return repo.replace_segments(conn, chapter_id, segments)


def _record(
    conn: Conn,
    session_id: str,
    chapter_id: int,
    kind: str,
    description: str,
    before: str,
    after: str,
) -> None:
    # Новая правка после отмены обрубает «будущее» — как везде.
    conn.execute(
        "DELETE FROM edit_op WHERE session_id = ? AND chapter_id = ? AND undone = 1",
        (session_id, chapter_id),
    )
    conn.execute(
        "INSERT INTO edit_op (session_id, chapter_id, kind, description, "
        "before_json, after_json) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, chapter_id, kind, description, before, after),
    )


@dataclass
class EditResult:
    segments: list[Segment]
    description: str
    can_undo: bool = True
    can_redo: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "description": self.description,
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
        }


def history_state(conn: Conn, session_id: str, chapter_id: int) -> dict[str, bool]:
    row = conn.execute(
        "SELECT SUM(undone = 0) AS done, SUM(undone = 1) AS undone FROM edit_op "
        "WHERE session_id = ? AND chapter_id = ?",
        (session_id, chapter_id),
    ).fetchone()
    return {
        "can_undo": bool(row["done"] or 0),
        "can_redo": bool(row["undone"] or 0),
    }


def _commit(
    conn: Conn,
    session_id: str,
    chapter_id: int,
    kind: str,
    description: str,
    before: str,
) -> EditResult:
    after = snapshot(conn, chapter_id)
    _record(conn, session_id, chapter_id, kind, description, before, after)
    state = history_state(conn, session_id, chapter_id)
    return EditResult(
        segments=repo.list_segments(conn, chapter_id),
        description=description,
        **state,
    )


# --------------------------------------------------------------------------
# Правки
# --------------------------------------------------------------------------


def assign_speaker(
    conn: Conn, session_id: str, chapter_id: int, segment_ids: Sequence[int], speaker: str
) -> EditResult:
    """Назначить говорящего выбранным сегментам (горячие клавиши 1–9)."""
    speaker = (speaker or "").strip() or NARRATOR
    if not segment_ids:
        raise EditError("не выбрано ни одного сегмента")
    before = snapshot(conn, chapter_id)
    for segment_id in segment_ids:
        repo.update_segment(conn, segment_id, speaker=speaker, is_manual=True)
    return _commit(
        conn, session_id, chapter_id, "assign",
        f"{_who(speaker)}: {_replicas(len(segment_ids))}", before,
    )


def assign_range(
    conn: Conn,
    session_id: str,
    chapter_id: int,
    char_start: int,
    char_end: int,
    speaker: str,
) -> EditResult:
    """Назначить говорящего произвольному выделению мышью.

    Сегменты, попавшие в выделение целиком, меняют говорящего; задетые краем —
    разрезаются по границе выделения.
    """
    if char_end <= char_start:
        raise EditError("пустое выделение")
    speaker = (speaker or "").strip() or NARRATOR

    before = snapshot(conn, chapter_id)
    existing = repo.list_segments(conn, chapter_id)
    rebuilt: list[Segment] = []
    touched = 0

    for segment in existing:
        start, end = segment.char_start, segment.char_end
        if start is None or end is None or end <= char_start or start >= char_end:
            rebuilt.append(segment)  # вне выделения — как было
            continue

        # Кусок до выделения.
        if start < char_start:
            rebuilt.append(_slice(segment, start, char_start))
        # Пересечение — ему и меняем говорящего.
        middle = _slice(segment, max(start, char_start), min(end, char_end))
        middle.speaker = speaker
        middle.is_manual = True
        middle.audio_path = None  # текст мог измениться — озвучка не годится
        middle.audio_hash = None
        rebuilt.append(middle)
        touched += 1
        # Кусок после выделения.
        if end > char_end:
            rebuilt.append(_slice(segment, char_end, end))

    # Текст выделения, который разметка пропустила (не попал ни в одну
    # реплику), становится новыми репликами — по абзацам, без пустых строк.
    text = repo.get_chapter(conn, chapter_id).text
    for start, end in _uncovered(existing, char_start, min(char_end, len(text))):
        for piece_start, piece_end in _paragraph_pieces(text, start, end):
            rebuilt.append(Segment(
                chapter_id=chapter_id, speaker=speaker, text=text[piece_start:piece_end],
                emotion=EMOTIONS[0], is_manual=True, char_start=piece_start, char_end=piece_end,
            ))
            touched += 1

    if not touched:
        raise EditError("в выделении нет текста")

    rebuilt.sort(key=lambda s: s.char_start if s.char_start is not None else 1 << 30)
    repo.replace_segments(conn, chapter_id, [s for s in rebuilt if s.text.strip()])
    return _commit(
        conn, session_id, chapter_id, "assign_range",
        f"{_who(speaker)}: {_replicas(touched)}", before,
    )


def _who(speaker: str) -> str:
    return "рассказчик" if speaker == NARRATOR else f"«{speaker}»"


def _replicas(count: int) -> str:
    mod10, mod100 = count % 10, count % 100
    word = "реплика" if mod10 == 1 and mod100 != 11 else (
        "реплики" if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14 else "реплик")
    return f"{count} {word}"


def _uncovered(segments: Sequence[Segment], start: int, end: int) -> list[tuple[int, int]]:
    """Куски [start, end), которые не покрыты ни одним сегментом."""
    spans = sorted(
        (s.char_start, s.char_end) for s in segments
        if s.char_start is not None and s.char_end is not None and s.char_end > start and s.char_start < end
    )
    out: list[tuple[int, int]] = []
    cursor = start
    for span_start, span_end in spans:
        if span_start > cursor:
            out.append((cursor, span_start))
        cursor = max(cursor, span_end)
    if cursor < end:
        out.append((cursor, end))
    return out


def _paragraph_pieces(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Непустые куски текста между переводами строк, без пробелов по краям."""
    pieces: list[tuple[int, int]] = []
    position = start
    for line in text[start:end].split("\n"):
        stripped = line.strip()
        # Тире и кавычки между репликами — не реплика: озвучивать там нечего.
        if any(ch.isalnum() for ch in stripped):
            begin = position + line.index(stripped)
            pieces.append((begin, begin + len(stripped)))
        position += len(line) + 1
    return pieces


def _slice(segment: Segment, start: int, end: int) -> Segment:
    """Кусок сегмента по абсолютным смещениям в тексте главы."""
    offset = segment.char_start or 0
    text = segment.text[start - offset : end - offset]
    return Segment(
        chapter_id=segment.chapter_id,
        speaker=segment.speaker,
        text=text,
        emotion=segment.emotion,
        is_manual=segment.is_manual,
        char_start=start,
        char_end=end,
        # Кусок целиком совпал с исходным — озвучка ещё годится.
        audio_path=segment.audio_path if text == segment.text else None,
        audio_hash=segment.audio_hash if text == segment.text else None,
    )


def split_segment(
    conn: Conn, session_id: str, chapter_id: int, segment_id: int, offset: int
) -> EditResult:
    """Разделить сегмент по позиции курсора внутри его текста."""
    segment = repo.get_segment(conn, segment_id)
    if segment.chapter_id != chapter_id:
        raise EditError("сегмент не из этой главы")
    text = segment.text
    if not 0 < offset < len(text):
        raise EditError("поставьте курсор внутрь фразы — на краю делить нечего")

    before = snapshot(conn, chapter_id)
    segments = repo.list_segments(conn, chapter_id)
    rebuilt: list[Segment] = []
    for item in segments:
        if item.id != segment_id:
            rebuilt.append(item)
            continue
        base = item.char_start
        left_end = base + offset if base is not None else None
        rebuilt.append(
            Segment(
                speaker=item.speaker, text=text[:offset], emotion=item.emotion,
                is_manual=True, char_start=base, char_end=left_end,
            )
        )
        rebuilt.append(
            Segment(
                speaker=item.speaker, text=text[offset:], emotion=item.emotion,
                is_manual=True, char_start=left_end, char_end=item.char_end,
            )
        )

    repo.replace_segments(conn, chapter_id, rebuilt)
    return _commit(conn, session_id, chapter_id, "split", "сегмент разделён", before)


def merge_segments(
    conn: Conn, session_id: str, chapter_id: int, segment_ids: Sequence[int]
) -> EditResult:
    """Склеить соседние сегменты одного говорящего."""
    if len(segment_ids) < 2:
        raise EditError("для склейки нужно хотя бы два сегмента")

    segments = repo.list_segments(conn, chapter_id)
    positions = {s.id: i for i, s in enumerate(segments)}
    chosen = sorted(segment_ids, key=lambda sid: positions.get(sid, 1 << 30))
    if any(sid not in positions for sid in chosen):
        raise EditError("сегмент не из этой главы")

    indexes = [positions[sid] for sid in chosen]
    if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        raise EditError("склеивать можно только соседние сегменты")
    speakers = {segments[i].speaker for i in indexes}
    if len(speakers) > 1:
        raise EditError(
            "у сегментов разные говорящие: " + ", ".join(sorted(speakers))
        )

    before = snapshot(conn, chapter_id)
    first, last = segments[indexes[0]], segments[indexes[-1]]
    merged = Segment(
        speaker=first.speaker,
        text=_merged_text(conn, chapter_id, [segments[i] for i in indexes]),
        emotion=first.emotion,
        is_manual=True,
        char_start=first.char_start,
        char_end=last.char_end,
    )
    rebuilt = segments[: indexes[0]] + [merged] + segments[indexes[-1] + 1 :]
    repo.replace_segments(conn, chapter_id, rebuilt)
    return _commit(
        conn, session_id, chapter_id, "merge",
        f"склеено: {_replicas(len(indexes))}", before,
    )


def _merged_text(conn: Conn, chapter_id: int, parts: Sequence[Segment]) -> str:
    """Текст склейки.

    Если куски шли в главе подряд, берём исходный отрезок — иначе на месте
    разреза посреди слова появился бы лишний пробел. Если между кусками был
    выброшенный текст (снятые кавычки, тире), склеиваем через пробел.
    """
    offsets_known = all(
        p.char_start is not None and p.char_end is not None for p in parts
    )
    contiguous = offsets_known and all(
        parts[i].char_end == parts[i + 1].char_start for i in range(len(parts) - 1)
    )
    if contiguous:
        chapter = repo.get_chapter(conn, chapter_id)
        return chapter.text[parts[0].char_start : parts[-1].char_end]
    return " ".join(p.text.strip() for p in parts).strip()


def rename_speaker(
    conn: Conn, session_id: str, chapter_id: int, old: str, new: str, whole_book: bool = False
) -> EditResult:
    """Массовое переназначение: все сегменты speaker=X становятся speaker=Y.

    Ради склейки дублей имён: «Руди» → «Рудеус» одним действием.
    """
    new = (new or "").strip()
    if not new:
        raise EditError("новое имя пустое")
    if old == new:
        raise EditError("имена совпадают")

    before = snapshot(conn, chapter_id)
    if whole_book:
        chapter = repo.get_chapter(conn, chapter_id)
        changed = conn.execute(
            "UPDATE segment SET speaker = ?, is_manual = 1 WHERE speaker = ? AND "
            "chapter_id IN (SELECT id FROM chapter WHERE book_id = ?)",
            (new, old, chapter.book_id),
        ).rowcount
        repo.rename_role_color(conn, chapter.book_id, old, new)
        # Слияние с ролью, у которой уже есть голос, — её голос остаётся:
        # «отдать реплики рассказчику» не должно менять голос рассказчика.
        taken = conn.execute(
            'SELECT 1 FROM "cast" WHERE book_id = ? AND speaker = ?', (chapter.book_id, new)
        ).fetchone()
        if taken:
            conn.execute('DELETE FROM "cast" WHERE book_id = ? AND speaker = ?', (chapter.book_id, old))
        else:
            conn.execute(
                'UPDATE "cast" SET speaker = ? WHERE book_id = ? AND speaker = ?',
                (new, chapter.book_id, old),
            )
        scope = "во всей книге"
    else:
        changed = conn.execute(
            "UPDATE segment SET speaker = ?, is_manual = 1 "
            "WHERE chapter_id = ? AND speaker = ?",
            (new, chapter_id, old),
        ).rowcount
        scope = "в главе"

    if not changed:
        raise EditError(f"реплик роли «{old}» не нашлось")
    return _commit(
        conn, session_id, chapter_id, "rename",
        f"{_who(old)} → {_who(new)} {scope}: {_replicas(changed)}", before,
    )


def set_emotion(
    conn: Conn, session_id: str, chapter_id: int, segment_ids: Sequence[int], emotion: str
) -> EditResult:
    if not segment_ids:
        raise EditError("не выбрано ни одного сегмента")
    before = snapshot(conn, chapter_id)
    for segment_id in segment_ids:
        repo.update_segment(conn, segment_id, emotion=emotion, is_manual=True)
    return _commit(
        conn, session_id, chapter_id, "emotion", f"эмоция «{emotion}»", before
    )


def set_text(
    conn: Conn, session_id: str, chapter_id: int, segment_id: int, text: str
) -> EditResult:
    """Правка текста реплики. Сбрасывает озвучку: она больше не соответствует."""
    text = (text or "").strip()
    if not text:
        raise EditError("текст сегмента не может быть пустым")
    before = snapshot(conn, chapter_id)
    repo.update_segment(
        conn, segment_id, text=text, is_manual=True, audio_path=None, audio_hash=None
    )
    return _commit(conn, session_id, chapter_id, "text", "текст изменён", before)


# --------------------------------------------------------------------------
# Отмена и повтор
# --------------------------------------------------------------------------


def undo(conn: Conn, session_id: str, chapter_id: int) -> EditResult:
    row = conn.execute(
        "SELECT * FROM edit_op WHERE session_id = ? AND chapter_id = ? AND undone = 0 "
        "ORDER BY id DESC LIMIT 1",
        (session_id, chapter_id),
    ).fetchone()
    if row is None:
        raise EditError("отменять нечего")
    _restore(conn, chapter_id, row["before_json"])
    conn.execute("UPDATE edit_op SET undone = 1 WHERE id = ?", (row["id"],))
    state = history_state(conn, session_id, chapter_id)
    return EditResult(
        segments=repo.list_segments(conn, chapter_id),
        description=f"отменено: {row['description']}",
        **state,
    )


def redo(conn: Conn, session_id: str, chapter_id: int) -> EditResult:
    row = conn.execute(
        "SELECT * FROM edit_op WHERE session_id = ? AND chapter_id = ? AND undone = 1 "
        "ORDER BY id ASC LIMIT 1",
        (session_id, chapter_id),
    ).fetchone()
    if row is None:
        raise EditError("повторять нечего")
    _restore(conn, chapter_id, row["after_json"])
    conn.execute("UPDATE edit_op SET undone = 0 WHERE id = ?", (row["id"],))
    state = history_state(conn, session_id, chapter_id)
    return EditResult(
        segments=repo.list_segments(conn, chapter_id),
        description=f"повторено: {row['description']}",
        **state,
    )
