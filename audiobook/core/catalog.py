"""Каталог голосов поверх базы: обновление у движков, прослушивание,
назначение ролям и профили озвучки.

Движки ничего не знают про базу, база ничего не знает про движки — связывает
их этот модуль.
"""

from __future__ import annotations

import difflib
import hashlib
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .. import paths
from . import gender as gender_mod
from . import repo
from .engines import EngineError, VoiceEngine, engine_names, get_engine
from .engines import elevenlabs as elevenlabs_engine
from .engines import qwen as qwen_engine
from .engines import silero as silero_engine
from .markup import DEFAULT_MODEL as DEFAULT_MARKUP_MODEL
from .models import NARRATOR, CastEntry, Voice

__all__ = [
    "DEFAULT_SETTINGS",
    "CatalogError",
    "settings",
    "save_settings",
    "engine_for",
    "engines_status",
    "refresh",
    "catalog",
    "preview",
    "cast_view",
    "assign",
    "auto_assign",
    "save_profile",
    "apply_profile",
]

Conn = sqlite3.Connection

DEFAULT_SETTINGS: dict[str, Any] = {
    "engine.silero.model": silero_engine.DEFAULT_MODEL,
    "engine.silero.device": "cpu",
    "engine.elevenlabs.model": elevenlabs_engine.DEFAULT_MODEL,
    # Папка Qwen: окружение и модель. Пусто — %LOCALAPPDATA%\BookTTS-qwen.
    "engine.qwen.home": "",
    "engine.qwen.model": qwen_engine.DEFAULT_MODEL,
    "preview.text": "Дождь кончился час назад, но крыши всё ещё роняли воду.",
    "synthesis.parallelism": 1,
    # Разметка по ролям. Адрес пустой — официальный API Anthropic (или
    # ANTHROPIC_BASE_URL из окружения, если он задан).
    "anthropic.base_url": "",
    "anthropic.model": DEFAULT_MARKUP_MODEL,
}

# Варианты для выпадающих списков в настройках. Там, где список зависит от
# аккаунта или от запущенного сервера, его здесь нет — только свободный ввод.
CHOICES: dict[str, list[str]] = {
    "engine.silero.model": list(silero_engine.MODELS),
    "engine.silero.device": ["cpu", "cuda"],
    # 1.7B звучит лучше, 0.6B вдвое легче для видеопамяти.
    "engine.qwen.model": [qwen_engine.DEFAULT_MODEL, "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"],
}

# Похожие имена персонажей при переносе профиля: «Велимир» и «Велемир» — один
# человек, «Елена» и «Алёна» — разные. Порог и проверка первой буквы те же,
# что при сведении имён в разметке.
SIMILARITY = 0.82


class CatalogError(RuntimeError):
    """Голос не прослушать или не назначить."""


# --------------------------------------------------------------------------
# Настройки и движки
# --------------------------------------------------------------------------


def settings(conn: Conn) -> dict[str, Any]:
    """Настройки приложения: значения по умолчанию, поверх — сохранённые."""
    return {**DEFAULT_SETTINGS, **repo.all_settings(conn)}


def save_settings(conn: Conn, values: dict[str, Any]) -> dict[str, Any]:
    """Сохранить настройки, проверив те, что могут сломать синтез."""
    if "engine.silero.device" in values:
        problem = silero_engine.device_problem(values["engine.silero.device"])
        if problem:
            # Иначе ошибка всплыла бы через полчаса, посреди озвучки книги.
            raise repo.InvalidOperation(problem)
    for key, value in values.items():
        if key in DEFAULT_SETTINGS or key.startswith(("engine.", "anthropic.")):
            if isinstance(value, str):
                value = value.strip()
            repo.set_setting(conn, key, value)
    return settings(conn)


def engine_for(conn: Conn, name: str) -> VoiceEngine:
    return get_engine(name, settings(conn))


def engines_status(conn: Conn) -> list[dict[str, Any]]:
    """Готовность каждого движка. Недоступный движок — не ошибка приложения."""
    options = settings(conn)
    out = []
    for name in engine_names():
        try:
            out.append(get_engine(name, options).status())
        except EngineError as exc:
            out.append({"name": name, "title": name, "ready": False, "detail": str(exc)})
    return out


# --------------------------------------------------------------------------
# Обновление каталога
# --------------------------------------------------------------------------


def refresh(conn: Conn, name: str | None = None) -> list[dict[str, Any]]:
    """Спросить у движков их голоса и обновить каталог.

    Список голосов всегда берётся у движка: у ElevenLabs он зависит от
    аккаунта, у Qwen — от того, что поднято на локальном сервере.
    """
    options = settings(conn)
    names = [name] if name else engine_names()
    report = []
    for engine_name in names:
        entry: dict[str, Any] = {"engine": engine_name, "added": 0, "updated": 0, "gone": 0}
        try:
            engine = get_engine(engine_name, options)
            voices = engine.list_voices()
        except EngineError as exc:
            entry["error"] = str(exc)
            report.append(entry)
            continue

        before = {v.voice_key for v in repo.list_voices(conn, engine=engine_name)}
        for info in voices:
            repo.upsert_voice(conn, Voice(
                engine=engine_name, voice_key=info.key, display_name=info.name or info.key,
                gender=info.gender, language=info.language, tags=",".join(info.tags),
                available=True,
            ))
        keys = [info.key for info in voices]
        entry["added"] = len([k for k in keys if k not in before])
        entry["updated"] = len(keys) - entry["added"]
        entry["gone"] = repo.mark_missing_voices(conn, engine_name, keys)
        report.append(entry)
    return report


def catalog(
    conn: Conn,
    engine: str | None = None,
    gender: str | None = None,
    language: str | None = None,
    search: str | None = None,
    available_only: bool = True,
) -> list[dict[str, Any]]:
    voices = repo.list_voices(
        conn, engine=engine, gender=gender, language=language,
        search=search, available_only=available_only,
    )
    return [{**voice.to_dict(), "preview_url": preview_url(voice)} for voice in voices]


def preview_url(voice: Voice) -> str:
    return f"/previews/{Path(voice.preview_path).name}" if voice.preview_path else ""


# --------------------------------------------------------------------------
# Прослушивание
# --------------------------------------------------------------------------


def preview(conn: Conn, voice_id: int, text: str = "", rate: float = 1.0,
            pitch: float = 1.0, volume: float = 1.0) -> dict[str, Any]:
    """Озвучить фразу этим голосом и вернуть путь к готовому файлу.

    Одинаковая фраза тем же голосом не пересинтезируется: имя файла — хеш от
    текста, голоса и настроек.
    """
    voice = repo.get_voice(conn, voice_id)
    options = settings(conn)
    phrase = (text or str(options.get("preview.text") or "")).strip()
    if not phrase:
        raise CatalogError("нечего прослушивать: пустая фраза")

    digest = hashlib.sha1(
        f"{voice.key}|{rate:g}|{pitch:g}|{volume:g}|{phrase}".encode("utf-8")
    ).hexdigest()[:16]
    paths.ensure_layout()
    existing = next(paths.previews_dir().glob(f"{voice.engine}-{digest}.*"), None)
    if existing is None:
        try:
            engine = get_engine(voice.engine, options)
            data, extension = engine.synthesize(
                phrase, voice.voice_key, rate=rate, pitch=pitch, volume=volume
            )
        except EngineError as exc:
            raise CatalogError(str(exc)) from exc
        existing = paths.previews_dir() / f"{voice.engine}-{digest}.{extension}"
        temporary = existing.with_suffix(existing.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(existing)  # в каталог попадает только целый файл
        repo.record_usage(conn, voice.engine, "preview", chars=len(phrase))

    relative = paths.relative(existing)
    is_default = not text or phrase == str(options.get("preview.text") or "")
    if is_default and abs(rate - 1) < 1e-3 and abs(pitch - 1) < 1e-3:
        repo.set_voice_preview(conn, voice.id, relative)
    return {"voice_id": voice.id, "path": relative, "url": f"/previews/{existing.name}",
            "text": phrase}


# --------------------------------------------------------------------------
# Роли книги
# --------------------------------------------------------------------------


def cast_view(conn: Conn, book_id: int) -> dict[str, Any]:
    """Персонажи книги с назначенными голосами — для экрана голосов."""
    book = repo.get_book(conn, book_id)
    assigned = repo.cast_map(conn, book_id)
    genders = speaker_genders(conn, book_id)
    speakers = []
    for name, count in repo.book_speakers(conn, book_id).items():
        entry = assigned.get(name)
        speakers.append({
            "name": name,
            "count": count,
            "is_narrator": name == NARRATOR,
            "gender": genders.get(name, {}).get("gender", ""),
            "gender_source": genders.get(name, {}).get("source", ""),
            "cast": entry.to_dict() if entry else None,
            "voice": entry.voice.to_dict() if entry and entry.voice else None,
            "preview_url": preview_url(entry.voice) if entry and entry.voice else "",
        })
    return {"book": book.to_dict(), "speakers": speakers}


def assign(conn: Conn, book_id: int, speaker: str, voice_id: int | None,
           rate: float = 1.0, pitch: float = 1.0, volume: float = 1.0) -> CastEntry:
    repo.get_book(conn, book_id)
    if voice_id is not None:
        repo.get_voice(conn, voice_id)  # 404, если голоса нет
    return repo.set_cast(conn, book_id, speaker, voice_id, rate, pitch, volume)


def speaker_genders(conn: Conn, book_id: int) -> dict[str, dict[str, Any]]:
    """Пол каждого персонажа книги — по глаголам рядом с его именем в тексте."""
    texts = [chapter.text for chapter in repo.list_chapters(conn, book_id)]
    return {
        speaker: gender_mod.evidence(speaker, texts)
        for speaker in repo.book_speakers(conn, book_id)
        if speaker != NARRATOR
    }


# Высота голоса для второй, третьей, четвёртой роли на одном голосе. Замерено
# на silero v5_5_ru: pitch 1.2 поднимает основной тон с 91 до 113 Гц, а вниз
# он почти не сдвигается (0.85 даёт 87 Гц) — поэтому только вверх.
PITCH_VARIANTS = (1.0, 1.15, 1.3, 1.08)


def _stable_index(name: str, size: int) -> int:
    """Индекс от хеша имени: персонаж звучит одинаково от запуска к запуску."""
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % max(size, 1)


def auto_assign(conn: Conn, book_id: int, engine: str = "silero",
                overwrite: bool = False) -> list[dict[str, Any]]:
    """Раздать голоса всем ролям книги.

    Рассказчику — ровный мужской, остальным — разные голоса, насколько хватает
    каталога. Выбор детерминированный: тот же персонаж получит тот же голос.
    """
    voices = repo.list_voices(conn, engine=engine, available_only=True)
    if not voices:
        raise CatalogError(
            f"в каталоге нет голосов движка {engine!r} — обновите каталог на экране голосов"
        )
    assigned = repo.cast_map(conn, book_id)
    narrator_voice = next((v for v in voices if v.gender == "м"), voices[0])
    others = [v for v in voices if v.id != narrator_voice.id] or voices
    genders = speaker_genders(conn, book_id)
    speakers = list(repo.book_speakers(conn, book_id))

    # Сколько ролей уже звучат каждым голосом — включая оставленные как есть.
    uses: Counter = Counter()
    taken: set[int] = set()
    for speaker, entry in assigned.items():
        if speaker in speakers and entry.voice_id and not overwrite:
            uses[entry.voice_id] += 1
            taken.add(entry.voice_id)

    result = []
    for speaker in speakers:
        if speaker in assigned and not overwrite:
            continue
        gender = genders.get(speaker, {}).get("gender", "")
        if speaker == NARRATOR:
            voice = narrator_voice
        else:
            # Сначала голоса того же пола: «сказал Терех» — значит, мужской.
            # Пол неизвестен — выбираем из всех, как раньше.
            pool = [v for v in others if v.gender == gender] if gender else others
            pool = pool or others
            free = [v for v in pool if v.id not in taken] or pool
            voice = free[_stable_index(speaker, len(free))]
            taken.add(voice.id)
        # Голос уже занят — тот же голос, но выше: два персонажа одного пола
        # в одной сцене иначе звучали бы одинаково.
        pitch = PITCH_VARIANTS[uses[voice.id] % len(PITCH_VARIANTS)]
        uses[voice.id] += 1
        repo.set_cast(conn, book_id, speaker, voice.id, pitch=pitch)
        result.append({"speaker": speaker, "voice": voice.to_dict(), "gender": gender,
                       "pitch": pitch, "shared": uses[voice.id] > 1})
    return result


# --------------------------------------------------------------------------
# Профили озвучки
# --------------------------------------------------------------------------


def save_profile(conn: Conn, book_id: int, name: str) -> dict[str, Any]:
    """Сохранить расклад голосов книги как профиль для других книг цикла."""
    entries = []
    for speaker, entry in repo.cast_map(conn, book_id).items():
        if entry.voice is None:
            continue
        entries.append({
            "speaker": speaker, "engine": entry.voice.engine, "voice_key": entry.voice.voice_key,
            "rate": entry.rate, "pitch": entry.pitch, "volume": entry.volume,
        })
    if not entries:
        raise CatalogError("в книге ещё нет назначенных голосов")
    return repo.save_profile(conn, name, entries)


def _same_person(left: str, right: str) -> bool:
    """«Велимир» и «Велемир» — один персонаж, «Елена» и «Алёна» — разные."""
    left, right = left.strip().lower(), right.strip().lower()
    if left == right:
        return True
    if not left or not right or left[0] != right[0]:
        return False
    return difflib.SequenceMatcher(None, left, right).ratio() >= SIMILARITY


def apply_profile(conn: Conn, book_id: int, profile_id: int,
                  overwrite: bool = True) -> dict[str, Any]:
    """Наложить профиль на книгу, сводя похожие имена персонажей."""
    profile = repo.get_profile(conn, profile_id)
    speakers = list(repo.book_speakers(conn, book_id))
    assigned = repo.cast_map(conn, book_id)

    applied, missing_voice, unmatched = [], [], []
    used_speakers: set[str] = set()
    for entry in profile["entries"]:
        match = next(
            (s for s in speakers if s not in used_speakers and _same_person(s, entry["speaker"])),
            None,
        )
        if match is None:
            unmatched.append(entry["speaker"])
            continue
        if match in assigned and not overwrite:
            continue
        voice = next(
            (v for v in repo.list_voices(conn, engine=entry["engine"])
             if v.voice_key == entry["voice_key"]),
            None,
        )
        if voice is None:
            missing_voice.append(f"{entry['speaker']}: {entry['engine']}/{entry['voice_key']}")
            continue
        repo.set_cast(conn, book_id, match, voice.id,
                      entry["rate"], entry["pitch"], entry["volume"])
        used_speakers.add(match)
        applied.append({"speaker": match, "from": entry["speaker"], "voice": voice.to_dict()})

    return {
        "profile": profile["name"],
        "applied": applied,
        # Персонажи профиля, которых нет в книге, и голоса, пропавшие из каталога.
        "unmatched": unmatched,
        "missing_voice": missing_voice,
        "without_voice": [s for s in speakers if s not in used_speakers and s not in assigned],
    }
