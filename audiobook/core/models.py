"""Датаклассы предметной области.

Ничего не знают ни про HTTP, ни про SQL: репозиторий собирает их из строк
базы, API сериализует через :meth:`to_dict`.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "Folder",
    "Book",
    "Chapter",
    "Segment",
    "Voice",
    "CastEntry",
    "Job",
    "EMOTIONS",
    "NARRATOR",
    "JobStatus",
    "segment_hash",
]

NARRATOR = "narrator"
EMOTIONS = ("нейтрально", "радостно", "зло", "грустно")


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


def segment_hash(text: str, voice_key: str) -> str:
    """Отпечаток озвучки сегмента.

    Совпал с ``segment.audio_hash`` — синтезировать заново нечего.
    Это и есть идемпотентность: меняется текст или голос — меняется хеш.
    """
    digest = hashlib.sha1()
    digest.update(voice_key.encode("utf-8"))
    digest.update(b"\n")
    digest.update((text or "").strip().encode("utf-8"))
    return digest.hexdigest()


def _row_to(cls, row: Any, **extra):
    if row is None:
        return None
    data = {k: row[k] for k in row.keys() if k in cls.__annotations__}
    data.update(extra)
    return cls(**data)


@dataclass
class Folder:
    id: int | None = None
    parent_id: int | None = None
    name: str = ""
    position: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row) -> "Folder":
        return _row_to(cls, row)


@dataclass
class Book:
    id: int | None = None
    folder_id: int | None = None
    title: str = ""
    author: str = ""
    source_path: str = ""  # относительный путь от корня библиотеки
    position: int = 0
    cover_path: str | None = None  # относительный путь
    language: str = "ru"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row) -> "Book":
        return _row_to(cls, row)


@dataclass
class Chapter:
    id: int | None = None
    book_id: int | None = None
    number: int = 0
    title: str = ""
    text: str = ""
    audio_path: str | None = None  # склеенная глава, относительный путь
    audio_hash: str | None = None
    duration_ms: int | None = None
    created_at: str = ""

    @property
    def label(self) -> str:
        return f"Глава {self.number}" + (f". {self.title}" if self.title else "")

    def to_dict(self, with_text: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label
        data["n_chars"] = len(self.text)
        if not with_text:
            data.pop("text")
        return data

    @classmethod
    def from_row(cls, row) -> "Chapter":
        return _row_to(cls, row)


@dataclass
class Segment:
    id: int | None = None
    chapter_id: int | None = None
    order: int = 0
    speaker: str = NARRATOR
    text: str = ""
    emotion: str = EMOTIONS[0]
    audio_path: str | None = None
    audio_hash: str | None = None
    is_manual: bool = False
    char_start: int | None = None
    char_end: int | None = None
    error: str | None = None
    audio_start_ms: int | None = None  # место в файле главы
    audio_end_ms: int | None = None

    @property
    def is_narrator(self) -> bool:
        return self.speaker == NARRATOR

    def needs_synthesis(self, voice_key: str) -> bool:
        """Нужен ли пересинтез при текущем тексте и голосе."""
        if not self.audio_path or not self.audio_hash:
            return True
        return self.audio_hash != segment_hash(self.text, voice_key)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_manual"] = bool(self.is_manual)
        return data

    @classmethod
    def from_row(cls, row) -> "Segment":
        keys = set(row.keys())
        return cls(
            id=row["id"],
            chapter_id=row["chapter_id"],
            order=row["order"],
            speaker=row["speaker"],
            text=row["text"],
            emotion=row["emotion"],
            audio_path=row["audio_path"],
            audio_hash=row["audio_hash"],
            is_manual=bool(row["is_manual"]),
            char_start=row["char_start"] if "char_start" in keys else None,
            char_end=row["char_end"] if "char_end" in keys else None,
            error=row["error"] if "error" in keys else None,
            audio_start_ms=row["audio_start_ms"] if "audio_start_ms" in keys else None,
            audio_end_ms=row["audio_end_ms"] if "audio_end_ms" in keys else None,
        )


@dataclass
class Voice:
    id: int | None = None
    engine: str = ""
    voice_key: str = ""
    display_name: str = ""
    gender: str = ""
    language: str = ""
    preview_path: str | None = None
    tags: str = ""  # через запятую: акцент, возраст, версия модели
    available: bool = True  # голос пропал из каталога движка, но роль на него ссылается
    updated_at: str | None = None

    @property
    def key(self) -> str:
        """Идентификатор для хеша озвучки."""
        return f"{self.engine}:{self.voice_key}"

    @property
    def label(self) -> str:
        return self.display_name or self.voice_key

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["key"] = self.key
        data["label"] = self.label
        data["available"] = bool(self.available)
        data["tags"] = [t for t in (self.tags or "").split(",") if t]
        return data

    @classmethod
    def from_row(cls, row) -> "Voice":
        voice = _row_to(cls, row)
        if voice is not None:
            # В SQLite это 0/1: без приведения сравнение с False не сработает.
            voice.available = bool(voice.available)
        return voice


@dataclass
class CastEntry:
    """Голос персонажа в конкретной книге, с настройками движка."""

    id: int | None = None
    book_id: int | None = None
    speaker: str = ""
    voice_id: int | None = None
    rate: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    voice: Voice | None = None  # подставляется репозиторием при чтении

    @property
    def voice_key(self) -> str:
        """Ключ для хеша: голос вместе с его настройками."""
        base = self.voice.key if self.voice else f"voice#{self.voice_id}"
        return f"{base}:{self.rate:g}:{self.pitch:g}:{self.volume:g}"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "book_id": self.book_id,
            "speaker": self.speaker,
            "voice_id": self.voice_id,
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
        }
        if self.voice:
            data["voice"] = self.voice.to_dict()
        return data


@dataclass
class Job:
    id: int | None = None
    kind: str = ""
    target_id: int | None = None
    status: str = JobStatus.PENDING
    progress: float = 0.0
    total: int = 0
    done: int = 0
    error: str | None = None
    title: str = ""
    payload: str = "{}"  # JSON: параметры, нужные для продолжения после перезапуска
    parent_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    notified: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def finished(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)

    @property
    def params(self) -> dict[str, Any]:
        import json

        try:
            value = json.loads(self.payload or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["notified"] = bool(self.notified)
        data["params"] = self.params
        return data

    @classmethod
    def from_row(cls, row) -> "Job":
        return _row_to(cls, row)
