"""Движки синтеза за одним интерфейсом.

Добавить движок — положить в эту папку один файл с функцией ``engine(options)``,
возвращающей объект с методами ``list_voices``, ``synthesize`` и
``supports_language``. Модули находятся сами: список нигде не дублируется.

Движки не знают ни про базу, ни про HTTP. Настройки (версия модели, адрес
локального сервера) приходят словарём ``options`` — их читает :mod:`catalog`.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "EngineError",
    "EngineFatalError",
    "VoiceInfo",
    "VoiceEngine",
    "get_engine",
    "engine_names",
    "all_engines",
]


class EngineError(RuntimeError):
    """Движок недоступен или не справился с синтезом. Повтор может помочь."""


class EngineFatalError(EngineError):
    """Повторять бессмысленно: нет ключа, нет такого голоса, нет CUDA.

    Отдельный класс нужен, чтобы очередь не тратила три попытки с паузами на
    ошибку, которая никогда не пройдёт сама.
    """


@dataclass
class VoiceInfo:
    """Голос в каталоге движка."""

    key: str  # идентификатор внутри движка
    name: str = ""
    gender: str = ""  # «м», «ж» или пусто
    language: str = ""  # «ru», «en», …
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name or self.key,
            "gender": self.gender, "language": self.language, "tags": list(self.tags),
        }


@runtime_checkable
class VoiceEngine(Protocol):
    """Что обязан уметь движок."""

    name: str
    title: str

    def list_voices(self) -> list[VoiceInfo]:
        """Голоса движка на сейчас. Не из памяти — спрашиваем сам движок."""

    def synthesize(
        self, text: str, voice_key: str, *, rate: float = 1.0,
        pitch: float = 1.0, volume: float = 1.0,
    ) -> tuple[bytes, str]:
        """Озвучить текст. Возвращает (данные файла, расширение)."""

    def supports_language(self, language: str) -> bool: ...

    def status(self) -> dict[str, Any]:
        """Готовность движка: чего не хватает, сколько осталось квоты."""


def _modules() -> dict[str, Any]:
    found: dict[str, Any] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(module, "engine"):
            found[info.name] = module
    return found


def engine_names() -> list[str]:
    return sorted(_modules())


def get_engine(name: str, options: dict[str, Any] | None = None) -> VoiceEngine:
    module = _modules().get(name)
    if module is None:
        raise EngineError(f"нет движка {name!r}; есть: {', '.join(engine_names())}")
    return module.engine(options or {})


def all_engines(options: dict[str, Any] | None = None) -> list[VoiceEngine]:
    """Все движки. ``options`` — общий словарь настроек, ключи вида ``engine.<имя>.<параметр>``."""
    return [get_engine(name, options) for name in engine_names()]
