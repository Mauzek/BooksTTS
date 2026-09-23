"""Silero: локальный синтез через torch.hub. Ключей не требует, в сеть ходит
только один раз — за самой моделью.

Списки дикторов сверены 18.09.2026 по файлам моделей в кеше torch.hub
(``torch.package`` внутри ``v5_5_ru.pt``), а не по документации.
"""

from __future__ import annotations

import io
import re
import wave
from typing import Any

from . import EngineError, EngineFatalError, VoiceInfo

NAME = "silero"
TITLE = "Silero (локально)"

DEFAULT_MODEL = "v5_5_ru"
# Порядок как в models.yml: новее — выше.
MODELS = ("v5_5_ru", "v5_4_ru", "v5_3_ru", "v5_2_ru", "v5_1_ru", "v5_ru", "v4_ru", "v3_1_ru")

# У всех русских моделей v3–v5 набор дикторов один и тот же. «random» из v4
# намеренно пропущен: он даёт разный голос на каждый вызов.
SPEAKERS = {
    "aidar": ("м", "ровный мужской, хорош для рассказчика"),
    "eugene": ("м", "мужской, живее aidar"),
    "baya": ("ж", "женский, низкий"),
    "kseniya": ("ж", "женский, мягкий"),
    "xenia": ("ж", "женский, звонкий"),
}

SAMPLE_RATE = 48000
MAX_CHUNK_CHARS = 800
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# Модель весит сотни мегабайт и грузится секунды — держим одну на процесс.
# Это кеш вычисления, а не состояние приложения: в базе ему делать нечего.
_LOADED: dict[tuple[str, str], Any] = {}


def device_problem(device: str) -> str:
    """Почему на этом устройстве синтез не пойдёт. Пустая строка — всё в порядке.

    Проверяем заранее: иначе «cuda» в настройках оборачивается невнятным
    «Torch not compiled with CUDA enabled» на каждой реплике.
    """
    if not str(device).lower().startswith("cuda"):
        return ""
    try:
        import torch
    except ImportError:  # pragma: no cover
        return "не установлен torch"
    if not torch.cuda.is_available():
        return (
            f"в этой сборке torch ({torch.__version__}) нет поддержки CUDA — "
            "выберите устройство cpu в настройках"
        )
    return ""


def split_for_tts(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Длинную реплику режем по предложениям: silero плохо ест длинные куски."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)

    out: list[str] = []
    for chunk in chunks:
        while len(chunk) > limit:  # одно предложение длиннее лимита — режем по словам
            cut = chunk.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            out.append(chunk)
    return out


def wav_bytes(samples, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return buffer.getvalue()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SileroEngine:
    name = NAME
    title = TITLE

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        self.model_version = options.get("engine.silero.model") or DEFAULT_MODEL
        self.device = options.get("engine.silero.device") or "cpu"
        self.sample_rate = int(options.get("engine.silero.sample_rate") or SAMPLE_RATE)

    # -- каталог ---------------------------------------------------------

    def list_voices(self) -> list[VoiceInfo]:
        loaded = _LOADED.get((self.model_version, self.device))
        names = [s for s in getattr(loaded, "speakers", ()) if s != "random"] or list(SPEAKERS)
        out = []
        for speaker in names:
            gender, note = SPEAKERS.get(speaker, ("", ""))
            out.append(VoiceInfo(
                key=speaker, name=speaker, gender=gender, language="ru",
                tags=[self.model_version] + ([note] if note else []),
            ))
        return out

    def supports_language(self, language: str) -> bool:
        return (language or "ru").lower().startswith("ru")

    def status(self) -> dict[str, Any]:
        base = {"name": self.name, "title": self.title, "needs_key": False,
                "variant": self.model_version}
        problem = device_problem(self.device)
        if problem:
            return {**base, "ready": False, "detail": problem}
        loaded = (self.model_version, self.device) in _LOADED
        return {
            **base, "ready": True,
            "detail": f"модель {self.model_version}"
            + ("" if loaded else ", загрузится при первом синтезе"),
        }

    # -- синтез ----------------------------------------------------------

    def _model(self):
        key = (self.model_version, self.device)
        if key in _LOADED:
            return _LOADED[key]
        problem = device_problem(self.device)
        if problem:
            # Повторять бессмысленно: видеокарта от повторов не появится.
            raise EngineFatalError(problem)
        try:
            import torch
        except ImportError as exc:  # pragma: no cover
            raise EngineError("для silero нужен torch: pip install -r requirements.txt") from exc
        try:
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models", model="silero_tts",
                language="ru", speaker=self.model_version, trust_repo=True,
            )
        except Exception as exc:  # noqa: BLE001 — torch.hub кидает что угодно
            raise EngineError(f"не загрузилась модель silero {self.model_version}: {exc}") from exc
        model.to(torch.device(self.device))
        _LOADED[key] = model
        return model

    def synthesize(
        self, text: str, voice_key: str, *, rate: float = 1.0,
        pitch: float = 1.0, volume: float = 1.0,
    ) -> tuple[bytes, str]:
        model = self._model()
        available = [s for s in getattr(model, "speakers", ())]
        if available and voice_key not in available:
            raise EngineFatalError(
                f"у модели {self.model_version} нет диктора {voice_key!r}; "
                f"есть: {', '.join(available)}"
            )
        chunks = split_for_tts(text)
        if not chunks:
            raise EngineError("пустой текст реплики")

        import torch

        waves = []
        for chunk in chunks:
            try:
                if abs(rate - 1.0) < 1e-3 and abs(pitch - 1.0) < 1e-3:
                    audio = model.apply_tts(
                        text=chunk, speaker=voice_key, sample_rate=self.sample_rate
                    )
                else:
                    # Темп и высота у silero задаются только через SSML.
                    prosody = f'rate="{int(rate * 100)}%"'
                    if abs(pitch - 1.0) >= 1e-3:
                        prosody += f' pitch="{int((pitch - 1) * 100):+d}%"'
                    audio = model.apply_tts(
                        ssml_text=f"<speak><prosody {prosody}>{_escape(chunk)}</prosody></speak>",
                        speaker=voice_key, sample_rate=self.sample_rate,
                    )
            except Exception as exc:  # noqa: BLE001 — модель кидает своё
                raise EngineError(f"silero не справился с репликой: {exc}") from exc
            waves.append(audio)

        audio = torch.cat(waves) if len(waves) > 1 else waves[0]
        if abs(volume - 1.0) >= 1e-3:
            audio = audio * float(volume)
        samples = (audio.clamp(-1.0, 1.0) * 32767).to(torch.int16).cpu().numpy()
        return wav_bytes(samples.tolist(), self.sample_rate), "wav"


def engine(options: dict[str, Any] | None = None) -> SileroEngine:
    return SileroEngine(options)
