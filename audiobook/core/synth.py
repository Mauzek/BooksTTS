"""Озвучка глав и сборка готового файла.

Идемпотентность: у каждой реплики хранится отпечаток ``hash(текст + голос)``.
Совпал — реплику не трогаем. Правка одной реплики, смена голоса персонажа или
исправление в словаре произношений переозвучивают ровно затронутое.

Ошибка одной реплики не останавливает главу: она записывается в ``segment.error``,
остальные озвучиваются дальше.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import paths
from . import assemble as assemble_mod
from . import catalog, pronounce, repo
from .engines import EngineError, EngineFatalError, uses_emotion
from .models import CastEntry, Segment, segment_hash

__all__ = [
    "SynthError",
    "plan",
    "synthesize_chapter",
    "assemble_chapter",
    "chapter_audio_dir",
    "chapter_signature",
]

log = logging.getLogger("audiobook.synth")

ATTEMPTS = 3
BACKOFF = (1.0, 3.0)  # пауза перед второй и третьей попыткой

Conn = sqlite3.Connection
Progress = Callable[[int, int], None]
ShouldStop = Callable[[], bool]


class SynthError(RuntimeError):
    """Главу нельзя озвучить: нет голосов или нечего озвучивать."""


def chapter_audio_dir(book_id: int, chapter_id: int) -> Path:
    return paths.audio_dir() / f"book-{book_id}" / f"chapter-{chapter_id}"


def chapter_signature(segments: Sequence[Segment]) -> str:
    """Отпечаток всей главы: по нему видно, что склейка устарела."""
    digest = hashlib.sha1()
    for segment in segments:
        digest.update((segment.audio_hash or "-").encode("utf-8"))
        digest.update(b"|")
    return digest.hexdigest()


def _cast_for(conn: Conn, book_id: int) -> dict[str, CastEntry]:
    return repo.cast_map(conn, book_id)


def plan(conn: Conn, chapter_id: int, force: bool = False) -> dict[str, Any]:
    """Что предстоит озвучить в главе — без единого обращения к движку."""
    chapter = repo.get_chapter(conn, chapter_id)
    cast = _cast_for(conn, chapter.book_id)
    rules = pronounce.compile_rules(repo.list_pronunciations(conn, chapter.book_id))
    segments = repo.list_segments(conn, chapter_id)

    missing = sorted({
        s.speaker for s in segments
        if s.speaker not in cast or cast[s.speaker].voice is None
    })
    pending: list[tuple[Segment, CastEntry, str, str]] = []
    emotional: dict[str, bool] = {}
    for segment in segments:
        entry = cast.get(segment.speaker)
        if entry is None or entry.voice is None:
            continue
        text = pronounce.apply(segment.text, rules)
        engine = entry.voice.engine
        if engine not in emotional:
            emotional[engine] = uses_emotion(engine)
        # Эмоция в отпечатке — только там, где она меняет звук. Иначе правка
        # эмоции объявила бы устаревшей озвучку Silero, которой она безразлична.
        fingerprint = f"{text}␞{segment.emotion}" if emotional[engine] else text
        key = segment_hash(fingerprint, entry.voice_key)
        if not force and segment.audio_hash == key and _has_audio(segment):
            continue
        pending.append((segment, entry, text, key))
    return {
        "chapter": chapter, "segments": segments, "pending": pending,
        "missing_voice": missing, "total": len(segments),
    }


def _has_audio(segment: Segment) -> bool:
    if not segment.audio_path:
        return False
    try:
        return paths.absolute(segment.audio_path).is_file()
    except paths.PathsError:
        return False


def synthesize_chapter(
    conn: Conn,
    chapter_id: int,
    *,
    parallelism: int = 1,
    force: bool = False,
    on_progress: Progress | None = None,
    should_stop: ShouldStop | None = None,
    commit_each: bool = True,
) -> dict[str, Any]:
    """Озвучить реплики главы. Готовые пропускаются.

    ``commit_each`` фиксирует каждую озвученную реплику отдельно. Это не
    мелочь: длинная глава идёт минутами, и без фиксаций ни прогресс не виден
    другим соединениям, ни продолжение после аварийного закрытия невозможно.
    """
    prepared = plan(conn, chapter_id, force=force)
    if prepared["missing_voice"]:
        raise SynthError(
            "нет голоса у: " + ", ".join(prepared["missing_voice"])
            + " — назначьте голоса на экране «Голоса»"
        )
    if not prepared["segments"]:
        raise SynthError("глава не размечена по ролям")

    chapter = prepared["chapter"]
    tasks = prepared["pending"]
    target_dir = chapter_audio_dir(chapter.book_id, chapter.id)
    target_dir.mkdir(parents=True, exist_ok=True)

    engines: dict[str, Any] = {}

    def engine_for(name: str):
        if name not in engines:
            engines[name] = catalog.engine_for(conn, name)
        return engines[name]

    def render(task) -> tuple[Any, bytes, str]:
        segment, entry, text, _key = task
        engine = engine_for(entry.voice.engine)
        # Эмоцию передаём только тем движкам, что её понимают.
        hints = {"emotion": segment.emotion} if getattr(engine, "uses_emotion", False) else {}
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            try:
                data, extension = engine.synthesize(
                    text, entry.voice.voice_key,
                    rate=entry.rate, pitch=entry.pitch, volume=entry.volume, **hints,
                )
                return task, data, extension
            except EngineFatalError as exc:
                # Ключ не появится, CUDA не установится — повторять нечего.
                raise SynthError(str(exc)) from exc
            except EngineError as exc:
                last = exc
                if attempt + 1 < ATTEMPTS:
                    time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        raise SynthError(str(last))

    done = 0
    failed: list[dict[str, Any]] = []
    total = len(tasks)
    cancelled = False

    def store(task, data: bytes, extension: str) -> None:
        segment, entry, text, key = task
        target = target_dir / f"{segment.order:04d}-{key[:12]}.{extension}"
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)  # в библиотеку попадает только целый файл
        if segment.audio_path:  # старая озвучка этой же реплики больше не нужна
            try:
                old = paths.absolute(segment.audio_path)
                if old != target:
                    old.unlink(missing_ok=True)
            except (paths.PathsError, OSError):
                pass
        repo.update_segment(
            conn, segment.id, audio_path=paths.relative(target), audio_hash=key,
            error=None, audio_start_ms=None, audio_end_ms=None,
        )
        repo.record_usage(
            conn, entry.voice.engine, "synthesis", chars=len(text), book_id=chapter.book_id
        )
        if commit_each:
            conn.commit()

    def fail(task, exc: Exception) -> None:
        segment = task[0]
        log.warning("реплика #%s не озвучена: %s", segment.id, exc)
        repo.update_segment(conn, segment.id, error=str(exc)[:500])
        failed.append({"segment_id": segment.id, "error": str(exc)})
        if commit_each:
            conn.commit()

    def report() -> None:
        if on_progress:
            on_progress(done, total)

    # Движки, которые умеют пачками (Qwen), получают реплики пачками: одна
    # фраза почти не нагружает видеокарту, пачка из восьми идёт в пять раз
    # быстрее. Остальные реплики — обычным путём ниже.
    batched: dict[str, list] = {}
    rest = []
    for task in tasks:
        name = task[1].voice.engine
        engine = engine_for(name)
        if getattr(engine, "batch_size", 1) > 1 and hasattr(engine, "synthesize_batch"):
            batched.setdefault(name, []).append(task)
        else:
            rest.append(task)

    for name, group in batched.items():
        engine = engine_for(name)
        for start in range(0, len(group), engine.batch_size):
            if should_stop and should_stop():
                cancelled = True
                break
            chunk = group[start : start + engine.batch_size]
            try:
                outputs: list = engine.synthesize_batch([
                    {"text": task[2], "voice_key": task[1].voice.voice_key,
                     "emotion": task[0].emotion}
                    for task in chunk
                ])
            except EngineFatalError as exc:
                outputs = [exc] * len(chunk)
            except EngineError:
                # Пачка упала целиком — повторяем по одной, чтобы одна кривая
                # реплика не губила семь нормальных.
                outputs = [None] * len(chunk)
            for task, output in zip(chunk, outputs):
                try:
                    if isinstance(output, tuple):
                        store(task, *output)
                    elif isinstance(output, Exception):
                        raise SynthError(str(output)) from output
                    else:
                        _task, data, extension = render(task)
                        store(task, data, extension)
                except Exception as exc:  # noqa: BLE001 — ошибка одной реплики
                    fail(task, exc)
                done += 1
                report()
        if cancelled:
            break
    tasks = [] if cancelled else rest

    if parallelism > 1 and tasks:
        # Движок зовём из нескольких потоков, а в базу пишем из одного:
        # соединение sqlite привязано к потоку.
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = {pool.submit(render, task): task for task in tasks}
            for future in concurrent.futures.as_completed(futures):
                task = futures[future]
                if should_stop and should_stop():
                    cancelled = True
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                try:
                    _task, data, extension = future.result()
                    store(task, data, extension)
                except Exception as exc:  # noqa: BLE001 — ошибка одной реплики
                    fail(task, exc)
                done += 1
                if on_progress:
                    on_progress(done, total)
    else:
        for task in tasks:
            if should_stop and should_stop():
                cancelled = True
                break
            try:
                _task, data, extension = render(task)
                store(task, data, extension)
            except Exception as exc:  # noqa: BLE001 — ошибка одной реплики
                fail(task, exc)
            done += 1
            if on_progress:
                on_progress(done, total)

    return {
        "chapter_id": chapter.id, "total": total, "done": done,
        "skipped": len(prepared["segments"]) - total,
        "failed": failed, "cancelled": cancelled,
    }


def assemble_chapter(conn: Conn, chapter_id: int, force: bool = False) -> dict[str, Any]:
    """Склеить озвученные реплики главы в один файл и запомнить раскладку."""
    chapter = repo.get_chapter(conn, chapter_id)
    book = repo.get_book(conn, chapter.book_id)
    segments = repo.list_segments(conn, chapter_id)
    if not segments:
        raise SynthError("глава не размечена по ролям")

    signature = chapter_signature(segments)
    if not force and chapter.audio_hash == signature and chapter.audio_path:
        try:
            if paths.absolute(chapter.audio_path).is_file():
                return {"chapter_id": chapter.id, "path": chapter.audio_path,
                        "duration_ms": chapter.duration_ms, "reused": True}
        except paths.PathsError:
            pass

    pieces = []
    for segment in segments:
        path = None
        error = ""
        if _has_audio(segment):
            path = paths.absolute(segment.audio_path)
        else:
            error = segment.error or "не озвучено"
        pieces.append(assemble_mod.Piece(
            speaker=segment.speaker, path=path, text=segment.text, error=error, id=segment.id
        ))

    out_dir = paths.audio_dir() / f"book-{book.id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = assemble_mod.assemble_chapter(
        pieces, out_dir / f"{chapter.number:03d}.mp3",
        book_title=book.title, author=book.author,
        chapter_number=chapter.number, chapter_title=chapter.title,
    )

    for entry in result.timeline:
        repo.update_segment(
            conn, entry["id"], audio_start_ms=entry["start_ms"], audio_end_ms=entry["end_ms"]
        )
    repo.update_chapter(
        conn, chapter.id, audio_path=paths.relative(result.path),
        audio_hash=signature, duration_ms=result.duration_ms,
    )
    return {
        "chapter_id": chapter.id, "path": paths.relative(result.path),
        "duration_ms": result.duration_ms, "used": result.used,
        "skipped": [p.text[:60] for p in result.skipped], "note": result.note,
        "reused": False,
    }
