"""Тесты синтеза. Настоящая модель не грузится — бэкенд подменён заглушкой."""

from __future__ import annotations

import io
import wave

import pytest

from audiobook.core.tts import (
    MAX_CHUNK_CHARS,
    SAMPLE_RATE,
    Synthesizer,
    TTSError,
    cache_key,
    concat_wav,
    split_for_tts,
    wav_bytes,
)
from audiobook.core.voices import Voice


class FakeBackend:
    """Отдаёт короткую тишину и считает вызовы."""

    name = "silero"
    sample_rate = SAMPLE_RATE

    def __init__(self, fail: str = "") -> None:
        self.calls: list[tuple[str, Voice]] = []
        self.fail = fail
        self.loads = 0

    def synthesize(self, text: str, voice: Voice) -> bytes:
        self.calls.append((text, voice))
        if self.fail:
            raise TTSError(self.fail)
        return wav_bytes([0] * 480, self.sample_rate)


def make(tmp_path, backend=None):
    backend = backend or FakeBackend()
    synth = Synthesizer(
        backends={"silero": backend}, cache_dir=tmp_path / "cache", show_progress=False
    )
    return synth, backend


def wav_frames(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as handle:
        return handle.getnframes()


# --------------------------------------------------------------------------
# Нарезка длинного текста
# --------------------------------------------------------------------------


def test_short_text_is_one_chunk():
    assert split_for_tts("Короткая реплика.") == ["Короткая реплика."]


def test_empty_text_gives_nothing():
    assert split_for_tts("   ") == []


def test_long_text_splits_on_sentences():
    text = ("Первое предложение. " * 80).strip()
    chunks = split_for_tts(text)
    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert all(c.endswith(".") for c in chunks)


def test_one_endless_sentence_splits_on_words():
    text = "слово " * 400
    chunks = split_for_tts(text)
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert "".join(c.replace(" ", "") for c in chunks) == text.replace(" ", "")


def test_nothing_is_lost_when_splitting():
    text = ("Предложение номер один. " * 60).strip()
    joined = " ".join(split_for_tts(text))
    assert joined.replace(" ", "") == text.replace(" ", "")


# --------------------------------------------------------------------------
# WAV
# --------------------------------------------------------------------------


def test_wav_round_trip():
    data = wav_bytes([0, 100, -100], SAMPLE_RATE)
    with wave.open(io.BytesIO(data), "rb") as handle:
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnchannels() == 1
        assert handle.getnframes() == 3


def test_concat_adds_up_frames():
    part = wav_bytes([0] * 100)
    assert wav_frames(concat_wav([part, part, part])) == 300


def test_concat_of_one_is_itself():
    part = wav_bytes([0] * 10)
    assert concat_wav([part]) == part


def test_concat_of_nothing_is_an_error():
    with pytest.raises(TTSError):
        concat_wav([])


# --------------------------------------------------------------------------
# Ключ кеша
# --------------------------------------------------------------------------


def test_same_text_and_voice_give_same_key():
    voice = Voice(speaker="baya")
    assert cache_key("Привет", voice) == cache_key("Привет", voice)


def test_voice_change_changes_the_key():
    assert cache_key("Привет", Voice(speaker="baya")) != cache_key(
        "Привет", Voice(speaker="xenia")
    )


def test_rate_change_changes_the_key():
    assert cache_key("Привет", Voice(speaker="baya")) != cache_key(
        "Привет", Voice(speaker="baya", rate=1.2)
    )


def test_text_change_changes_the_key():
    voice = Voice()
    assert cache_key("Привет", voice) != cache_key("Пока", voice)


# --------------------------------------------------------------------------
# Синтез и кеш
# --------------------------------------------------------------------------


def test_synthesis_writes_a_file(tmp_path):
    synth, backend = make(tmp_path)
    result = synth.synthesize("Реплика", Voice())
    assert result.ok and result.path.is_file()
    assert len(backend.calls) == 1


def test_second_call_comes_from_cache(tmp_path):
    synth, backend = make(tmp_path)
    synth.synthesize("Реплика", Voice())
    again = synth.synthesize("Реплика", Voice())
    assert again.cached is True
    assert len(backend.calls) == 1  # модель не трогали второй раз


def test_changed_voice_is_synthesized_again(tmp_path):
    synth, backend = make(tmp_path)
    synth.synthesize("Реплика", Voice(speaker="baya"))
    synth.synthesize("Реплика", Voice(speaker="xenia"))
    assert len(backend.calls) == 2


def test_backend_failure_is_reported_not_raised(tmp_path):
    synth, _ = make(tmp_path, FakeBackend(fail="модель упала"))
    result = synth.synthesize("Реплика", Voice())
    assert not result.ok
    assert "модель упала" in result.error
    assert not result.path.exists()  # битый файл в кеш не попадает


def test_empty_line_is_reported(tmp_path):
    synth, backend = make(tmp_path)
    result = synth.synthesize("   ", Voice())
    assert not result.ok
    assert backend.calls == []


def test_many_lines_reuse_one_backend(tmp_path):
    synth, backend = make(tmp_path)
    items = [(f"Реплика {i}", Voice()) for i in range(5)]
    results = synth.synthesize_many(items)
    assert all(r.ok for r in results)
    assert len(backend.calls) == 5
    assert len({r.path for r in results}) == 5


def test_repeated_line_is_synthesized_once(tmp_path):
    synth, backend = make(tmp_path)
    synth.synthesize_many([("Одно и то же", Voice())] * 4)
    assert len(backend.calls) == 1
