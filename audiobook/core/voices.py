"""Голоса персонажей: чтение voices.yaml, подсказки, проверка назначений.

Голос не назначается сам собой: синтез не начнётся, пока для каждой роли
не выбран голос — в панели ролей приложения или руками в voices.yaml.
Подсказка при этом детерминированная: одно имя — один и тот же голос при
каждом запуске, чтобы персонаж не менял звучание между главами.

Формат voices.yaml::

    narrator: { engine: silero, speaker: aidar, rate: 1.0 }
    Рудеус:   { engine: silero, speaker: eugene, rate: 1.05 }
    default:  { engine: silero, speaker: baya }
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "Voice",
    "VoiceMap",
    "VoicesError",
    "SILERO_SPEAKERS",
    "ELEVENLABS_ENGINE",
    "SILERO_ENGINE",
    "DEFAULT_VOICES_PATH",
]

SILERO_ENGINE = "silero"
ELEVENLABS_ENGINE = "elevenlabs"
ENGINES = (SILERO_ENGINE, ELEVENLABS_ENGINE)

# Дикторы модели v4_ru. «random» намеренно не берём: он даёт разный голос
# на каждый вызов и ломает единство персонажа.
SILERO_SPEAKERS = ("aidar", "baya", "kseniya", "xenia", "eugene")

# Рассказчику — ровный мужской голос; персонажам — всё остальное.
NARRATOR_SPEAKER = "aidar"
CHARACTER_POOL = ("eugene", "baya", "kseniya", "xenia", "aidar")

DEFAULT_VOICES_PATH = Path("voices.yaml")

MIN_RATE, MAX_RATE = 0.5, 2.0


class VoicesError(RuntimeError):
    """voices.yaml не читается или содержит недопустимый голос."""


@dataclass(frozen=True)
class Voice:
    engine: str = SILERO_ENGINE
    speaker: str = NARRATOR_SPEAKER
    rate: float = 1.0

    def __post_init__(self) -> None:
        if self.engine not in ENGINES:
            raise VoicesError(
                f"неизвестный движок {self.engine!r}; доступны: {', '.join(ENGINES)}"
            )
        if self.engine == SILERO_ENGINE and self.speaker not in SILERO_SPEAKERS:
            raise VoicesError(
                f"у silero нет диктора {self.speaker!r}; "
                f"доступны: {', '.join(SILERO_SPEAKERS)}"
            )
        if not MIN_RATE <= self.rate <= MAX_RATE:
            raise VoicesError(
                f"темп {self.rate} вне диапазона {MIN_RATE}–{MAX_RATE}"
            )

    @property
    def key(self) -> str:
        """Идентификатор голоса для ключа кеша синтеза."""
        return f"{self.engine}:{self.speaker}:{self.rate:g}"

    def to_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "speaker": self.speaker, "rate": self.rate}

    @classmethod
    def from_dict(cls, data: Any, where: str = "") -> "Voice":
        if isinstance(data, str):  # краткая запись: просто имя диктора
            return cls(speaker=data)
        if not isinstance(data, dict):
            raise VoicesError(
                f"{where or 'голос'}: ожидался словарь или имя диктора, "
                f"а не {type(data).__name__}"
            )
        try:
            rate = float(data.get("rate", 1.0))
        except (TypeError, ValueError) as exc:
            raise VoicesError(f"{where or 'голос'}: темп не число") from exc
        try:
            return cls(
                engine=str(data.get("engine", SILERO_ENGINE)),
                speaker=str(data.get("speaker", NARRATOR_SPEAKER)),
                rate=rate,
            )
        except VoicesError as exc:
            raise VoicesError(f"{where or 'голос'}: {exc}") from exc


def _stable_index(name: str, size: int) -> int:
    """Индекс от хеша имени: один персонаж — один голос между запусками."""
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % max(size, 1)


class VoiceMap:
    """Назначенные голоса плюс детерминированные подсказки для остальных."""

    def __init__(
        self,
        entries: dict[str, Voice] | None = None,
        default: Voice | None = None,
        path: Path | None = None,
    ) -> None:
        self.entries: dict[str, Voice] = dict(entries or {})
        self.default = default
        self.path = path

    # -- чтение и запись -------------------------------------------------

    @classmethod
    def load(cls, path: str | Path = DEFAULT_VOICES_PATH) -> "VoiceMap":
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise VoicesError(
                "для voices.yaml нужен PyYAML: pip install -r requirements.txt"
            ) from exc
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise VoicesError(f"{path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise VoicesError(f"{path.name}: ожидался список «роль: голос»")

        entries: dict[str, Voice] = {}
        default: Voice | None = None
        for name, value in raw.items():
            name = str(name)
            voice = Voice.from_dict(value, where=f"{path.name}, роль {name!r}")
            if name == "default":
                default = voice
            else:
                entries[name] = voice
        return cls(entries=entries, default=default, path=path)

    def save(self, path: str | Path | None = None) -> Path:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise VoicesError(
                "для voices.yaml нужен PyYAML: pip install -r requirements.txt"
            ) from exc

        target = Path(path or self.path or DEFAULT_VOICES_PATH)
        payload: dict[str, Any] = {
            name: voice.to_dict() for name, voice in self.entries.items()
        }
        if self.default:
            payload["default"] = self.default.to_dict()

        header = (
            "# Голоса персонажей. engine: silero | elevenlabs\n"
            f"# Дикторы silero: {', '.join(SILERO_SPEAKERS)}\n"
            f"# rate — темп речи, {MIN_RATE}–{MAX_RATE}\n"
        )
        body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(header + body, encoding="utf-8")
        tmp.replace(target)
        self.path = target
        return target

    # -- назначения ------------------------------------------------------

    def get(self, speaker: str) -> Voice | None:
        """Явно назначенный голос. ``None`` — роль ещё не озвучена."""
        voice = self.entries.get(speaker)
        if voice is None and self.default is not None:
            return self.default
        return voice

    def assign(self, speaker: str, voice: Voice) -> None:
        self.entries[speaker] = voice

    def suggest(self, speaker: str, taken: Iterable[str] = ()) -> Voice:
        """Предложить голос: рассказчику — свой, остальным — из свободных."""
        if speaker == "narrator":
            return Voice(speaker=NARRATOR_SPEAKER)

        used = {v.speaker for v in self.entries.values() if v.engine == SILERO_ENGINE}
        used.update(taken)
        free = [s for s in CHARACTER_POOL if s not in used]
        # Голосов меньше, чем персонажей — это нормально, начинаем круг заново.
        pool = free or list(CHARACTER_POOL)
        return Voice(speaker=pool[_stable_index(speaker, len(pool))])

    def resolve(self, speaker: str) -> Voice:
        """Голос для синтеза: назначенный, иначе подсказка."""
        return self.get(speaker) or self.suggest(speaker)

    def unassigned(self, speakers: Sequence[str]) -> list[str]:
        """Роли без явного голоса — синтез до их назначения не начинаем."""
        if self.default is not None:
            return []
        return [s for s in dict.fromkeys(speakers) if s not in self.entries]

    def suggestions_for(self, speakers: Sequence[str]) -> dict[str, Voice]:
        """Готовый набор «роль -> голос» для панели ролей и для voices.yaml."""
        out: dict[str, Voice] = {}
        taken: set[str] = set()
        for speaker in dict.fromkeys(speakers):
            existing = self.entries.get(speaker)
            voice = existing or self.suggest(speaker, taken=taken)
            out[speaker] = voice
            if voice.engine == SILERO_ENGINE:
                taken.add(voice.speaker)
        return out

    def with_rate(self, speaker: str, rate: float) -> Voice:
        return replace(self.resolve(speaker), rate=rate)

    def __contains__(self, speaker: object) -> bool:
        return speaker in self.entries

    def __len__(self) -> int:
        return len(self.entries)
