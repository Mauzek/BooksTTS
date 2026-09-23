"""Синтез речи: silero локально, elevenlabs опционально — за общим интерфейсом.

Модель грузится один раз на процесс, а не на каждую реплику. Результат
кешируется по хешу ``текст + голос``: повторный прогон главы ничего не
пересинтезирует, и правка одной реплики не заставляет переозвучивать остальные.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from .voices import ELEVENLABS_ENGINE, SILERO_ENGINE, Voice

__all__ = [
    "TTSError",
    "Backend",
    "SileroBackend",
    "ElevenLabsBackend",
    "Synthesizer",
    "SynthesisResult",
    "DEFAULT_CACHE_DIR",
    "SAMPLE_RATE",
]

log = logging.getLogger("audiobook.tts")

SAMPLE_RATE = 48000
DEFAULT_CACHE_DIR = Path("cache") / "tts"

# Версия модели silero. У v4_ru и v5_5_ru одни и те же пять дикторов, но v5
# звучит заметно чище — у v4 слышен металлический призвук, особенно у aidar.
# Версия входит в ключ кеша: после переключения реплики переозвучиваются.
SILERO_MODEL = "v5_5_ru"
SILERO_MODELS = ("v5_5_ru", "v5_4_ru", "v5_3_ru", "v5_2_ru", "v5_1_ru", "v4_ru", "v3_1_ru")

# Silero плохо переваривает очень длинные куски: режем по предложениям.
MAX_CHUNK_CHARS = 800


class TTSError(RuntimeError):
    """Синтез невозможен или не удался."""


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def split_for_tts(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Разбить длинную реплику на куски по границам предложений."""
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

    # Одно предложение длиннее лимита — режем по словам, иначе синтез упадёт.
    out: list[str] = []
    for chunk in chunks:
        while len(chunk) > limit:
            cut = chunk.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            out.append(chunk)
    return out


def wav_bytes(samples: Sequence[int], sample_rate: int = SAMPLE_RATE) -> bytes:
    """Собрать WAV (16 бит, моно) из целочисленных отсчётов."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return buffer.getvalue()


def concat_wav(parts: Sequence[bytes]) -> bytes:
    """Склеить несколько WAV одного формата в один — без внешних зависимостей."""
    if not parts:
        raise TTSError("нечего склеивать: пустой список кусков")
    if len(parts) == 1:
        return parts[0]

    frames: list[bytes] = []
    params = None
    for part in parts:
        with wave.open(io.BytesIO(part), "rb") as handle:
            if params is None:
                params = handle.getparams()
            frames.append(handle.readframes(handle.getnframes()))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(params.nchannels)
        handle.setsampwidth(params.sampwidth)
        handle.setframerate(params.framerate)
        handle.writeframes(b"".join(frames))
    return buffer.getvalue()


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# --------------------------------------------------------------------------
# Бэкенды
# --------------------------------------------------------------------------


class Backend(Protocol):
    """Общий интерфейс: текст плюс голос — на выходе WAV."""

    name: str
    sample_rate: int

    def synthesize(self, text: str, voice: Voice) -> bytes: ...


class SileroBackend:
    """Локальный синтез silero v4_ru через torch.hub. Модель грузится один раз."""

    name = SILERO_ENGINE

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        device: str = "cpu",
        model_version: str = SILERO_MODEL,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.model_version = model_version
        # Попадает в ключ кеша: сменили модель — озвучка пересчитается.
        self.variant = model_version
        self._model = None

    @property
    def speakers(self) -> tuple[str, ...]:
        return tuple(getattr(self._load(), "speakers", ()))

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
        except ImportError as exc:  # pragma: no cover
            raise TTSError(
                "для silero нужен torch: pip install -r requirements.txt"
            ) from exc

        log.info("загружаю silero %s (первый раз — скачивание)", self.model_version)
        try:
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker=self.model_version,
                trust_repo=True,
            )
        except Exception as exc:  # noqa: BLE001 — torch.hub кидает что угодно
            raise TTSError(
                f"не загрузилась модель silero {self.model_version}: {exc}"
            ) from exc
        model.to(torch.device(self.device))
        self._model = model
        return model

    def synthesize(self, text: str, voice: Voice) -> bytes:
        model = self._load()
        available = tuple(getattr(model, "speakers", ()))
        if available and voice.speaker not in available:
            raise TTSError(
                f"у модели {self.model_version} нет диктора {voice.speaker!r}; "
                f"есть: {', '.join(available)}"
            )
        chunks = split_for_tts(text)
        if not chunks:
            raise TTSError("пустой текст реплики")

        import torch

        waves = []
        for chunk in chunks:
            try:
                if abs(voice.rate - 1.0) < 1e-3:
                    audio = model.apply_tts(
                        text=chunk,
                        speaker=voice.speaker,
                        sample_rate=self.sample_rate,
                    )
                else:
                    # Темп у silero задаётся только через SSML.
                    ssml = (
                        f'<speak><prosody rate="{int(voice.rate * 100)}%">'
                        f"{_xml_escape(chunk)}</prosody></speak>"
                    )
                    audio = model.apply_tts(
                        ssml_text=ssml,
                        speaker=voice.speaker,
                        sample_rate=self.sample_rate,
                    )
            except Exception as exc:  # noqa: BLE001 — модель кидает что угодно
                raise TTSError(f"silero не справился с репликой: {exc}") from exc
            waves.append(audio)

        audio = torch.cat(waves) if len(waves) > 1 else waves[0]
        samples = (audio.clamp(-1.0, 1.0) * 32767).to(torch.int16).cpu().numpy()
        return wav_bytes(samples.tolist(), self.sample_rate)


class ElevenLabsBackend:
    """Облачный синтез. Ключ — из .env; следим за лимитом бесплатного тарифа."""

    name = ELEVENLABS_ENGINE
    # На бесплатном тарифе 10 000 символов в месяц — считаем и предупреждаем.
    FREE_TIER_CHARS = 10_000

    def __init__(
        self,
        api_key: str | None = None,
        sample_rate: int = 44100,
        char_budget: int | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.sample_rate = sample_rate
        self.char_budget = self.FREE_TIER_CHARS if char_budget is None else char_budget
        self.chars_used = 0
        self._client = None

    def _load(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise TTSError(
                "нет ELEVENLABS_API_KEY в окружении или .env — "
                "используйте engine: silero"
            )
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError as exc:  # pragma: no cover
            raise TTSError(
                "для elevenlabs нужен пакет elevenlabs: pip install elevenlabs"
            ) from exc
        self._client = ElevenLabs(api_key=self.api_key)
        return self._client

    def synthesize(self, text: str, voice: Voice) -> bytes:
        if self.char_budget and self.chars_used + len(text) > self.char_budget:
            raise TTSError(
                f"исчерпан лимит elevenlabs: {self.chars_used} из {self.char_budget} "
                "символов. Переключите роль на silero или поднимите лимит."
            )
        client = self._load()
        try:
            stream = client.text_to_speech.convert(
                voice_id=voice.speaker,
                text=text,
                output_format="pcm_44100",
            )
            pcm = b"".join(stream)
        except Exception as exc:  # noqa: BLE001 — SDK кидает своё
            raise TTSError(f"elevenlabs не ответил: {exc}") from exc

        self.chars_used += len(text)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(pcm)
        return buffer.getvalue()


# --------------------------------------------------------------------------
# Кеш и фасад
# --------------------------------------------------------------------------


@dataclass
class SynthesisResult:
    """Одна озвученная реплика."""

    path: Path
    voice: Voice
    text: str
    cached: bool
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def cache_key(text: str, voice: Voice, variant: str = "") -> str:
    """Ключ кеша: текст, голос и версия модели.

    Без версии смена модели молча отдавала бы старую озвучку из кеша.
    """
    digest = hashlib.sha1()
    digest.update(voice.key.encode("utf-8"))
    if variant:
        digest.update(f"|{variant}".encode("utf-8"))
    digest.update(b"\n")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


class Synthesizer:
    """Общая точка входа: выбирает бэкенд по голосу и кеширует результат."""

    def __init__(
        self,
        backends: dict[str, Backend] | None = None,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        show_progress: bool = True,
        silero_model: str = SILERO_MODEL,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.show_progress = show_progress
        self.silero_model = silero_model
        self._backends: dict[str, Backend] = dict(backends or {})

    def backend_for(self, voice: Voice) -> Backend:
        if voice.engine not in self._backends:
            if voice.engine == SILERO_ENGINE:
                self._backends[voice.engine] = SileroBackend(
                    model_version=self.silero_model
                )
            elif voice.engine == ELEVENLABS_ENGINE:
                self._backends[voice.engine] = ElevenLabsBackend()
            else:  # pragma: no cover — Voice уже проверил движок
                raise TTSError(f"нет бэкенда для движка {voice.engine!r}")
        return self._backends[voice.engine]

    def variant_for(self, voice: Voice) -> str:
        """Метка версии модели, попадающая в ключ кеша."""
        return getattr(self.backend_for(voice), "variant", "")

    def path_for(self, text: str, voice: Voice) -> Path:
        key = cache_key(text.strip(), voice, self.variant_for(voice))
        return self.cache_dir / f"{key}.wav"

    def synthesize(self, text: str, voice: Voice) -> SynthesisResult:
        """Озвучить реплику. Готовый файл из кеша не пересинтезируется."""
        text = (text or "").strip()
        target = self.path_for(text, voice)
        if target.exists() and target.stat().st_size > 44:
            return SynthesisResult(target, voice, text, cached=True)
        if not text:
            return SynthesisResult(target, voice, text, cached=False, error="пустой текст")

        try:
            audio = self.backend_for(voice).synthesize(text, voice)
        except TTSError as exc:
            log.error("не озвучено (%s): %s", voice.key, exc)
            return SynthesisResult(target, voice, text, cached=False, error=str(exc))

        tmp = target.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(target)  # в кеш попадает только целый файл
        return SynthesisResult(target, voice, text, cached=False)

    def synthesize_many(
        self, items: Iterable[tuple[str, Voice]], desc: str = "Синтез"
    ) -> list[SynthesisResult]:
        """Озвучить пачку реплик, переиспользуя загруженную модель."""
        items = list(items)
        iterator: Iterable[tuple[str, Voice]] = items
        if self.show_progress and items:
            try:
                from tqdm import tqdm

                iterator = tqdm(items, desc=desc, unit="реплика", leave=False)
            except ImportError:
                pass
        return [self.synthesize(text, voice) for text, voice in iterator]
