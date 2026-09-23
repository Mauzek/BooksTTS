"""Склейка озвученных реплик в один файл главы.

Паузы: 400 мс при смене говорящего, 150 мс между репликами одного и того же.
Громкость выравнивается до склейки — иначе разные дикторы silero звучат
заметно тише или громче друг друга.

Для mp3 нужен ffmpeg. Системный не обязателен: если его нет в PATH, берётся
бинарник из пакета imageio-ffmpeg. Если нет и его — пишем wav и говорим об этом.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

__all__ = [
    "AssembleError",
    "Piece",
    "AssemblyResult",
    "assemble_chapter",
    "ensure_ffmpeg",
    "GAP_SWITCH_MS",
    "GAP_SAME_MS",
]

log = logging.getLogger("audiobook.assemble")

GAP_SWITCH_MS = 400  # между репликами разных говорящих
GAP_SAME_MS = 150  # между репликами одного говорящего
TARGET_DBFS = -20.0  # к этому уровню подтягиваем каждую реплику
HEADROOM_DB = 1.0  # запас до клиппинга у итогового файла
SILENCE_FLOOR_DBFS = -50.0  # тише — считаем тишиной и не усиливаем


class AssembleError(RuntimeError):
    """Склейка невозможна."""


@dataclass
class Piece:
    """Озвученная реплика: кто говорит и где лежит её wav."""

    speaker: str
    path: Path | None = None
    text: str = ""
    error: str = ""
    id: int | None = None  # сегмент, к которому относится: по нему строится раскладка

    @property
    def ok(self) -> bool:
        return not self.error and self.path is not None and Path(self.path).is_file()


@dataclass
class AssemblyResult:
    path: Path
    duration_ms: int
    used: int
    skipped: list[Piece] = field(default_factory=list)
    format: str = "mp3"
    note: str = ""
    # Где какая реплика звучит в готовом файле: [{id, start_ms, end_ms}].
    # По этому плеер подсвечивает текущий сегмент и перематывает по клику.
    timeline: list[dict] = field(default_factory=list)

    @property
    def duration_str(self) -> str:
        seconds = self.duration_ms // 1000
        return f"{seconds // 60}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def ensure_ffmpeg() -> str | None:
    """Найти ffmpeg и подсунуть его pydub. ``None`` — не нашли."""
    try:
        from pydub import AudioSegment
    except ImportError as exc:  # pragma: no cover
        raise AssembleError(
            "для склейки нужен pydub: pip install -r requirements.txt"
        ) from exc

    found = shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg

            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 — пакета может не быть
            found = None

    if found:
        AudioSegment.converter = found
        AudioSegment.ffmpeg = found
    return found


# --------------------------------------------------------------------------
# Склейка
# --------------------------------------------------------------------------


def _leveled(segment, target_dbfs: float = TARGET_DBFS):
    """Подтянуть реплику к общему уровню, не трогая тишину."""
    if segment.dBFS == float("-inf") or segment.dBFS < SILENCE_FLOOR_DBFS:
        return segment
    return segment.apply_gain(target_dbfs - segment.dBFS)


def assemble_chapter(
    pieces: Sequence[Piece],
    out_path: str | Path,
    *,
    book_title: str = "",
    author: str = "",
    chapter_number: int | None = None,
    chapter_title: str = "",
    gap_switch_ms: int = GAP_SWITCH_MS,
    gap_same_ms: int = GAP_SAME_MS,
    normalize: bool = True,
    bitrate: str = "128k",
) -> AssemblyResult:
    """Склеить реплики главы в один файл.

    Реплики без файла (не озвучились) пропускаются и возвращаются в
    ``skipped`` — молча терять кусок главы нельзя.
    """
    try:
        from pydub import AudioSegment
        from pydub import effects
    except ImportError as exc:  # pragma: no cover
        raise AssembleError(
            "для склейки нужен pydub: pip install -r requirements.txt"
        ) from exc

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = ensure_ffmpeg()
    fmt = out_path.suffix.lstrip(".").lower() or "mp3"
    note = ""
    if fmt == "mp3" and not ffmpeg:
        fmt = "wav"
        out_path = out_path.with_suffix(".wav")
        note = (
            "ffmpeg не найден — сохранено в wav. "
            "Поставьте ffmpeg или pip install imageio-ffmpeg для mp3."
        )
        log.warning(note)

    track = None
    skipped: list[Piece] = []
    timeline: list[dict] = []
    used = 0
    previous_speaker: str | None = None

    for piece in pieces:
        if not piece.ok:
            skipped.append(piece)
            continue
        try:
            segment = AudioSegment.from_file(str(piece.path))
        except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять главу
            skipped.append(Piece(piece.speaker, piece.path, piece.text, str(exc), piece.id))
            continue

        segment = _leveled(segment)
        if track is None:
            track = segment[:0]  # пустая дорожка того же формата
        elif used:
            gap = gap_same_ms if piece.speaker == previous_speaker else gap_switch_ms
            track += AudioSegment.silent(duration=gap, frame_rate=segment.frame_rate)

        start = len(track)
        track += segment
        timeline.append({"id": piece.id, "start_ms": start, "end_ms": len(track)})
        previous_speaker = piece.speaker
        used += 1

    if track is None or not used:
        raise AssembleError("нечего склеивать: ни одна реплика не озвучена")

    if normalize:
        track = effects.normalize(track, headroom=HEADROOM_DB)

    tags = {
        key: value
        for key, value in {
            "album": book_title,
            "artist": author,
            "title": _chapter_label(chapter_number, chapter_title),
            "track": str(chapter_number) if chapter_number else "",
        }.items()
        if value
    }

    params = {"format": fmt}
    if fmt == "mp3":
        params["bitrate"] = bitrate
        params["tags"] = tags
    try:
        track.export(str(out_path), **params)
    except Exception as exc:  # noqa: BLE001 — ffmpeg кидает своё
        raise AssembleError(f"не удалось сохранить {out_path.name}: {exc}") from exc

    return AssemblyResult(
        path=out_path,
        duration_ms=len(track),
        used=used,
        skipped=skipped,
        format=fmt,
        note=note,
        timeline=timeline,
    )


def _chapter_label(number: int | None, title: str) -> str:
    if number and title:
        return f"Глава {number}. {title}"
    if number:
        return f"Глава {number}"
    return title
