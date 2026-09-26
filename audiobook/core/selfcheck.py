"""Самопроверка собранного бэкенда: всё ли нужное попало в exe.

Запуск бэкенда ничего не доказывает: сборка 0.3.0 запускалась, отвечала на
запросы — и не озвучивала ни одной реплики, потому что в неё не попал numpy.
Здесь по очереди прогоняется то, без чего приложение бесполезно: путь звука
от тензора до mp3, поиск по библиотеке, хранилище ключей, файлы интерфейса.
С ``synth=True`` ещё и настоящий синтез silero (первый раз качает модель).
"""

from __future__ import annotations

import io
import math
import sqlite3
import wave
from dataclasses import dataclass
from typing import Callable

__all__ = ["Check", "run"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _wav_frames(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as handle:
        return handle.getnframes()


def _mp3_from_wav(data: bytes) -> int:
    """Перекодировать wav в mp3 так же, как при склейке. Возвращает размер mp3."""
    from pydub import AudioSegment

    from .assemble import ensure_ffmpeg

    if not ensure_ffmpeg():
        raise RuntimeError("ffmpeg не найден")
    out = io.BytesIO()
    AudioSegment.from_file(io.BytesIO(data), format="wav").export(out, format="mp3")
    size = len(out.getvalue())
    if not size:
        raise RuntimeError("ffmpeg вернул пустой mp3")
    return size


def check_tensor_to_mp3() -> str:
    import torch

    from .engines.silero import tensor_to_wav

    rate = 24000
    tone = torch.sin(torch.arange(rate // 4) * (2 * math.pi * 440 / rate)) * 0.5
    data = tensor_to_wav(tone, rate)
    frames = _wav_frames(data)
    if frames != rate // 4:
        raise RuntimeError(f"в wav {frames} отсчётов вместо {rate // 4}")
    return f"torch {torch.__version__}, mp3 {_mp3_from_wav(data)} байт"


def check_bundled_ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def check_fts5() -> str:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    finally:
        conn.close()
    return f"sqlite {sqlite3.sqlite_version}"


def check_keyring() -> str:
    import keyring

    backend = keyring.get_keyring()
    name = type(backend).__module__ + "." + type(backend).__name__
    if "fail" in name.lower() or "null" in name.lower():
        raise RuntimeError(f"нет рабочего хранилища ключей ({name})")
    return name


def check_ui_files() -> str:
    from ..api.app import WEB_DIR
    from .engines.qwen import WORKER

    missing = [str(p) for p in (WEB_DIR / "index.html", WORKER) if not p.is_file()]
    if missing:
        raise RuntimeError("нет файлов: " + ", ".join(missing))
    return str(WEB_DIR)


def check_silero_synthesis() -> str:
    from .engines.silero import SileroEngine

    data, _ext = SileroEngine().synthesize("Проверка связи. Один, два, три.", "xenia")
    frames = _wav_frames(data)
    if frames < 4800:  # короче 0,1 с при 48 кГц — это не речь
        raise RuntimeError(f"подозрительно короткий звук: {frames} отсчётов")
    return f"{frames / 48000:.1f} с речи, mp3 {_mp3_from_wav(data)} байт"


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("звук: тензор → wav → mp3", check_tensor_to_mp3),
    ("ffmpeg из сборки", check_bundled_ffmpeg),
    ("поиск по библиотеке (FTS5)", check_fts5),
    ("хранилище ключей", check_keyring),
    ("файлы интерфейса", check_ui_files),
]


def run(synth: bool = False) -> list[Check]:
    checks = CHECKS + ([("синтез silero", check_silero_synthesis)] if synth else [])
    results = []
    for name, func in checks:
        try:
            results.append(Check(name, True, func()))
        except Exception as exc:  # noqa: BLE001 — любая поломка сборки идёт в отчёт
            results.append(Check(name, False, f"{type(exc).__name__}: {exc}"))
    return results
