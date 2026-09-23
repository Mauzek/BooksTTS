"""Экспорт книги наружу: m4b с оглавлением, mp3 по главам, разметка в JSON.

m4b — формат аудиокниг: один файл, внутри оглавление и обложка, плееры умеют
прыгать по главам. Собирается ffmpeg-ом: список файлов и метаданные глав
передаются ему отдельными входами.

Разметка в JSON нужна, чтобы поделиться готовым «кастингом» книги: кто какую
реплику говорит и каким голосом. Импорт привязывается к тексту, а не к номерам
строк, поэтому переживает мелкие правки текста.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import paths
from . import repo
from .assemble import ensure_ffmpeg
from .models import Segment

__all__ = [
    "ExportError",
    "export_m4b",
    "export_mp3",
    "export_markup",
    "import_markup",
    "book_export_dir",
    "MARKUP_FORMAT",
]

MARKUP_FORMAT = "booktts-markup"
MARKUP_VERSION = 1
AAC_BITRATE = "64k"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExportError(RuntimeError):
    """Экспорт невозможен: нечего выгружать или ffmpeg не справился."""


def safe_name(name: str, fallback: str = "книга") -> str:
    """Имя файла, которое примет Windows."""
    cleaned = _UNSAFE.sub(" ", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or fallback


def book_export_dir(book_id: int, title: str) -> Path:
    directory = paths.exports_dir() / f"{book_id:03d} {safe_name(title)}".strip()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _voiced_chapters(conn: sqlite3.Connection, book_id: int) -> list:
    chapters = [c for c in repo.list_chapters(conn, book_id) if c.audio_path]
    if not chapters:
        raise ExportError("в книге нет озвученных глав — сначала озвучьте её")
    return chapters


def _audio_path(relative: str) -> Path:
    path = paths.absolute(relative)
    if not path.is_file():
        raise ExportError(f"файл озвучки не найден: {relative}")
    return path


# --------------------------------------------------------------------------
# mp3 по главам
# --------------------------------------------------------------------------


def export_mp3(
    conn: sqlite3.Connection, book_id: int, on_progress: Callable[[int, int], None] | None = None
) -> dict[str, Any]:
    """Разложить главы отдельными mp3 с понятными именами.

    Теги ID3 проставлены при склейке, поэтому файлы просто копируются.
    """
    book = repo.get_book(conn, book_id)
    chapters = _voiced_chapters(conn, book_id)
    target = book_export_dir(book.id, book.title)

    files = []
    for index, chapter in enumerate(chapters, start=1):
        source = _audio_path(chapter.audio_path)
        name = f"{chapter.number:03d}. {safe_name(chapter.title or f'Глава {chapter.number}')}.mp3"
        destination = target / name
        shutil.copy2(source, destination)
        files.append(paths.relative(destination))
        if on_progress:
            on_progress(index, len(chapters))
    return {"kind": "mp3", "dir": paths.relative(target), "files": files}


# --------------------------------------------------------------------------
# m4b
# --------------------------------------------------------------------------


def _concat_list(chapters: Sequence) -> str:
    lines = ["ffconcat version 1.0"]
    for chapter in chapters:
        path = str(_audio_path(chapter.audio_path)).replace("\\", "/")
        # В списке ffmpeg апостроф закрывает строку — экранируем по их правилам.
        lines.append("file '" + path.replace("'", r"'\''") + "'")
    return "\n".join(lines) + "\n"


def _ffmetadata(book, chapters: Sequence) -> str:
    lines = [";FFMETADATA1", f"title={book.title}", f"artist={book.author or ''}",
             f"album={book.title}", "genre=Audiobook"]
    position = 0
    for chapter in chapters:
        duration = int(chapter.duration_ms or 0)
        lines += [
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={position}", f"END={position + duration}",
            f"title={chapter.label}",
        ]
        position += duration
    return "\n".join(lines) + "\n"


def export_m4b(
    conn: sqlite3.Connection, book_id: int, on_progress: Callable[[int, int], None] | None = None
) -> dict[str, Any]:
    """Собрать книгу в один m4b с оглавлением и обложкой."""
    book = repo.get_book(conn, book_id)
    chapters = _voiced_chapters(conn, book_id)
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        raise ExportError(
            "для m4b нужен ffmpeg: поставьте его в систему или pip install imageio-ffmpeg"
        )

    target_dir = book_export_dir(book.id, book.title)
    output = target_dir / f"{safe_name(book.title)}.m4b"
    work = target_dir / ".build"
    work.mkdir(exist_ok=True)
    listing = work / "chapters.txt"
    metadata = work / "meta.txt"
    listing.write_text(_concat_list(chapters), encoding="utf-8")
    metadata.write_text(_ffmetadata(book, chapters), encoding="utf-8")
    if on_progress:
        on_progress(1, 3)

    cover = None
    if book.cover_path:
        try:
            candidate = paths.absolute(book.cover_path)
            cover = candidate if candidate.is_file() else None
        except paths.PathsError:
            cover = None

    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(metadata),
    ]
    if cover:
        command += ["-i", str(cover)]
    command += ["-map", "0:a", "-map_metadata", "1"]
    if cover:
        # Обложка внутри файла: плееры показывают её как картинку книги.
        command += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    command += ["-c:a", "aac", "-b:a", AAC_BITRATE, "-movflags", "+faststart", str(output)]

    if on_progress:
        on_progress(2, 3)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError(f"ffmpeg не запустился: {exc}") from exc
    if completed.returncode != 0 or not output.is_file():
        raise ExportError(f"ffmpeg не собрал m4b: {(completed.stderr or '')[-400:]}")

    shutil.rmtree(work, ignore_errors=True)
    if on_progress:
        on_progress(3, 3)
    total = sum(int(c.duration_ms or 0) for c in chapters)
    return {
        "kind": "m4b", "path": paths.relative(output), "chapters": len(chapters),
        "duration_ms": total, "size": output.stat().st_size, "cover": bool(cover),
    }


# --------------------------------------------------------------------------
# Разметка в JSON
# --------------------------------------------------------------------------


def _text_digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def markup_payload(conn: sqlite3.Connection, book_id: int) -> dict[str, Any]:
    book = repo.get_book(conn, book_id)
    cast = []
    for speaker, entry in repo.cast_map(conn, book_id).items():
        if entry.voice is None:
            continue
        cast.append({
            "speaker": speaker, "engine": entry.voice.engine, "voice_key": entry.voice.voice_key,
            "rate": entry.rate, "pitch": entry.pitch, "volume": entry.volume,
        })
    chapters = []
    for chapter in repo.list_chapters(conn, book_id):
        segments = repo.list_segments(conn, chapter.id)
        if not segments:
            continue
        chapters.append({
            "number": chapter.number, "title": chapter.title,
            "text_digest": _text_digest(chapter.text),
            "segments": [
                {
                    "start": s.char_start, "end": s.char_end, "speaker": s.speaker,
                    "emotion": s.emotion, "is_manual": bool(s.is_manual), "text": s.text,
                }
                for s in segments
            ],
        })
    if not chapters:
        raise ExportError("в книге нет размеченных глав")
    return {
        "format": MARKUP_FORMAT, "version": MARKUP_VERSION,
        "book": {"title": book.title, "author": book.author},
        "cast": cast,
        "pronunciations": repo.list_pronunciations(conn, book_id),
        "chapters": chapters,
    }


def export_markup(conn: sqlite3.Connection, book_id: int) -> dict[str, Any]:
    book = repo.get_book(conn, book_id)
    payload = markup_payload(conn, book_id)
    target = book_export_dir(book.id, book.title) / f"{safe_name(book.title)}.markup.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "kind": "json", "path": paths.relative(target),
        "chapters": len(payload["chapters"]), "cast": len(payload["cast"]),
    }


def _segments_for(
    chapter, entries: Sequence[dict[str, Any]], digest: str = ""
) -> tuple[list[Segment], int]:
    """Наложить сегменты из файла на текст главы.

    Если текст не менялся, смещения берутся как есть. Если менялся — каждую
    реплику ищем в тексте: разметка переживает правку опечаток.
    """
    text = chapter.text
    segments: list[Segment] = []
    lost = 0
    cursor = 0
    exact = bool(digest) and _text_digest(text) == digest
    for entry in entries:
        fragment = (entry.get("text") or "").strip()
        start, end = entry.get("start"), entry.get("end")
        if exact and isinstance(start, int) and isinstance(end, int) and text[start:end] == fragment:
            found = start
        else:
            found = text.find(fragment, cursor) if fragment else -1
            if found < 0:
                found = text.find(fragment) if fragment else -1
        if found < 0:
            lost += 1
            continue
        segments.append(Segment(
            speaker=entry.get("speaker") or "narrator",
            text=fragment,
            emotion=entry.get("emotion") or "нейтрально",
            is_manual=bool(entry.get("is_manual")),
            char_start=found, char_end=found + len(fragment),
        ))
        cursor = found + len(fragment)
    return segments, lost


def import_markup(
    conn: sqlite3.Connection, book_id: int, payload: dict[str, Any], with_cast: bool = True
) -> dict[str, Any]:
    """Наложить разметку из JSON на книгу: главы сопоставляются по номерам."""
    if not isinstance(payload, dict) or payload.get("format") != MARKUP_FORMAT:
        raise ExportError("это не файл разметки BookTTS")
    if int(payload.get("version") or 0) > MARKUP_VERSION:
        raise ExportError("файл сделан более новой версией приложения")

    repo.get_book(conn, book_id)
    by_number = {c.number: c for c in repo.list_chapters(conn, book_id)}
    applied, skipped, lost_total = [], [], 0

    for entry in payload.get("chapters", []):
        chapter = by_number.get(entry.get("number"))
        if chapter is None:
            skipped.append(entry.get("number"))
            continue
        segments, lost = _segments_for(
            chapter, entry.get("segments") or [], entry.get("text_digest") or ""
        )
        lost_total += lost
        if not segments:
            skipped.append(chapter.number)
            continue
        repo.replace_segments(conn, chapter.id, segments)
        applied.append({"number": chapter.number, "segments": len(segments), "lost": lost})

    cast_applied, cast_missing = 0, []
    if with_cast:
        for item in payload.get("cast", []):
            voice = next(
                (v for v in repo.list_voices(conn, engine=item.get("engine"))
                 if v.voice_key == item.get("voice_key")),
                None,
            )
            if voice is None:
                cast_missing.append(f"{item.get('speaker')}: {item.get('engine')}/{item.get('voice_key')}")
                continue
            repo.set_cast(
                conn, book_id, item["speaker"], voice.id,
                float(item.get("rate", 1.0)), float(item.get("pitch", 1.0)),
                float(item.get("volume", 1.0)),
            )
            cast_applied += 1

    for rule in payload.get("pronunciations", []):
        if rule.get("term"):
            repo.set_pronunciation(
                conn, book_id, rule["term"], rule.get("replacement", ""),
                whole_word=bool(rule.get("whole_word", True)),
                case_sensitive=bool(rule.get("case_sensitive")),
            )

    return {
        "chapters": applied, "skipped": skipped, "lost_segments": lost_total,
        "cast": cast_applied, "cast_missing": cast_missing,
    }
