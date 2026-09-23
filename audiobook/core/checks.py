"""Проверки разметки перед синтезом.

Это не ошибки, а поводы посмотреть глазами: почти всё, что здесь находится,
бывает и законным. Поэтому проверки ничего не чинят и ничего не блокируют,
кроме отсутствующих голосов — без них синтезировать просто нечем.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import repo
from .models import NARRATOR

__all__ = ["Finding", "check_chapter", "check_book", "RARE_SPEAKER_LINES", "LONG_NARRATOR_CHARS"]

Conn = sqlite3.Connection

# Персонаж с одной-двумя репликами на книгу — обычно ошибка разметки:
# имя из обращения приняли за говорящего.
RARE_SPEAKER_LINES = 2

# Кусок рассказчика длиннее этого посреди диалога подозрителен: вероятно,
# в него затянуло чью-то реплику.
LONG_NARRATOR_CHARS = 400


@dataclass
class Finding:
    kind: str          # no_voice | rare_speaker | long_narrator
    severity: str      # blocker | warning
    message: str
    segment_ids: list[int] = field(default_factory=list)
    speaker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "segment_ids": self.segment_ids,
            "speaker": self.speaker,
        }


def _no_voice(conn: Conn, book_id: int, segments) -> list[Finding]:
    cast = repo.cast_map(conn, book_id)
    without: dict[str, list[int]] = {}
    for segment in segments:
        entry = cast.get(segment.speaker)
        if entry is None or entry.voice_id is None:
            without.setdefault(segment.speaker, []).append(segment.id)
    return [
        Finding(
            kind="no_voice",
            severity="blocker",
            message=f"«{speaker}»: голос не назначен, {len(ids)} сегм.",
            segment_ids=ids,
            speaker=speaker,
        )
        for speaker, ids in sorted(without.items(), key=lambda kv: -len(kv[1]))
    ]


def _rare_speakers(conn: Conn, book_id: int, segments) -> list[Finding]:
    counts = repo.book_speakers(conn, book_id)
    findings: list[Finding] = []
    for speaker, total in counts.items():
        if speaker == NARRATOR or total > RARE_SPEAKER_LINES:
            continue
        ids = [s.id for s in segments if s.speaker == speaker]
        if not ids:
            continue
        findings.append(
            Finding(
                kind="rare_speaker",
                severity="warning",
                message=(
                    f"«{speaker}»: всего {total} реплик(и) на книгу — "
                    "возможно, это не персонаж"
                ),
                segment_ids=ids,
                speaker=speaker,
            )
        )
    return findings


def _long_narrator(segments) -> list[Finding]:
    findings: list[Finding] = []
    for index, segment in enumerate(segments):
        if segment.speaker != NARRATOR or len(segment.text) <= LONG_NARRATOR_CHARS:
            continue
        neighbours = segments[max(0, index - 1) : index] + segments[index + 1 : index + 2]
        # Длинное описание между репликами персонажей — вот что подозрительно.
        if not any(n.speaker != NARRATOR for n in neighbours):
            continue
        findings.append(
            Finding(
                kind="long_narrator",
                severity="warning",
                message=(
                    f"рассказчик на {len(segment.text)} символов посреди диалога — "
                    "возможно, внутри осталась чья-то реплика"
                ),
                segment_ids=[segment.id],
                speaker=NARRATOR,
            )
        )
    return findings


def check_chapter(conn: Conn, chapter_id: int) -> list[Finding]:
    chapter = repo.get_chapter(conn, chapter_id)
    segments = repo.list_segments(conn, chapter_id)
    if not segments:
        return [
            Finding("no_markup", "blocker", "глава ещё не размечена по ролям")
        ]
    return (
        _no_voice(conn, chapter.book_id, segments)
        + _rare_speakers(conn, chapter.book_id, segments)
        + _long_narrator(segments)
    )


def check_book(conn: Conn, book_id: int) -> dict[int, list[Finding]]:
    return {
        chapter.id: check_chapter(conn, chapter.id)
        for chapter in repo.list_chapters(conn, book_id)
    }
