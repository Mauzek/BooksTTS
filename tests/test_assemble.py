"""Тесты склейки главы. Синтез не запускается — на вход подаются готовые wav."""

from __future__ import annotations

import pytest

from audiobook.core.assemble import (
    GAP_SAME_MS,
    GAP_SWITCH_MS,
    AssembleError,
    Piece,
    assemble_chapter,
    ensure_ffmpeg,
)
from audiobook.core.tts import wav_bytes

pytest.importorskip("pydub")

RATE = 48000


def tone(path, ms: int = 1000, amplitude: int = 8000):
    """Небеззвучный wav: на тишине нормализация ничего не показала бы."""
    count = int(RATE * ms / 1000)
    samples = [amplitude if (i // 120) % 2 == 0 else -amplitude for i in range(count)]
    path.write_bytes(wav_bytes(samples, RATE))
    return path


@pytest.fixture()
def pieces(tmp_path):
    return [
        Piece("narrator", tone(tmp_path / "a.wav")),
        Piece("narrator", tone(tmp_path / "b.wav")),
        Piece("Аглая", tone(tmp_path / "c.wav")),
    ]


# --------------------------------------------------------------------------
# Паузы
# --------------------------------------------------------------------------


def test_pauses_differ_for_same_and_other_speaker(tmp_path, pieces):
    result = assemble_chapter(pieces, tmp_path / "out.wav", normalize=False)
    # 1000 + 150 (тот же голос) + 1000 + 400 (смена голоса) + 1000
    assert result.duration_ms == 3000 + GAP_SAME_MS + GAP_SWITCH_MS
    assert result.used == 3


def test_pauses_are_configurable(tmp_path, pieces):
    result = assemble_chapter(
        pieces, tmp_path / "out.wav", gap_same_ms=0, gap_switch_ms=0, normalize=False
    )
    assert result.duration_ms == 3000


def test_single_piece_has_no_trailing_pause(tmp_path):
    piece = Piece("narrator", tone(tmp_path / "a.wav"))
    result = assemble_chapter([piece], tmp_path / "out.wav", normalize=False)
    assert result.duration_ms == 1000


# --------------------------------------------------------------------------
# Пропуски
# --------------------------------------------------------------------------


def test_unsynthesized_lines_are_reported(tmp_path):
    good = Piece("narrator", tone(tmp_path / "a.wav"))
    bad = Piece("Аглая", None, "не озвучилось", error="бэкенд упал")
    result = assemble_chapter([good, bad], tmp_path / "out.wav")
    assert result.used == 1
    assert [p.speaker for p in result.skipped] == ["Аглая"]


def test_missing_file_is_skipped_not_fatal(tmp_path):
    good = Piece("narrator", tone(tmp_path / "a.wav"))
    ghost = Piece("Аглая", tmp_path / "нет.wav")
    result = assemble_chapter([good, ghost], tmp_path / "out.wav")
    assert result.used == 1 and len(result.skipped) == 1


def test_broken_wav_is_skipped_not_fatal(tmp_path):
    good = Piece("narrator", tone(tmp_path / "a.wav"))
    broken = tmp_path / "broken.wav"
    broken.write_bytes("это не wav".encode("utf-8"))
    result = assemble_chapter([good, Piece("Аглая", broken)], tmp_path / "out.wav")
    assert result.used == 1
    assert result.skipped[0].error


def test_nothing_to_assemble_is_an_error(tmp_path):
    with pytest.raises(AssembleError, match="ни одна реплика"):
        assemble_chapter([Piece("Аглая", None, error="упало")], tmp_path / "out.wav")


# --------------------------------------------------------------------------
# Экспорт
# --------------------------------------------------------------------------


def test_wav_export_is_readable(tmp_path, pieces):
    from pydub import AudioSegment

    result = assemble_chapter(pieces, tmp_path / "out.wav")
    assert result.format == "wav"
    assert len(AudioSegment.from_wav(str(result.path))) == result.duration_ms


def test_quiet_and_loud_lines_are_levelled(tmp_path):
    from pydub import AudioSegment

    loud = Piece("narrator", tone(tmp_path / "loud.wav", amplitude=20000))
    quiet = Piece("narrator", tone(tmp_path / "quiet.wav", amplitude=500))
    result = assemble_chapter([loud, quiet], tmp_path / "out.wav", gap_same_ms=0)

    track = AudioSegment.from_wav(str(result.path))
    first, second = track[:1000], track[1000:]
    assert abs(first.dBFS - second.dBFS) < 3  # до выравнивания разрыв ~32 дБ


def test_mp3_export_has_id3_tags(tmp_path, pieces):
    if not ensure_ffmpeg():
        pytest.skip("ffmpeg недоступен")
    result = assemble_chapter(
        pieces,
        tmp_path / "out.mp3",
        book_title="Проба",
        author="Автор",
        chapter_number=7,
        chapter_title="Ночной рынок",
    )
    assert result.format == "mp3"
    head = result.path.read_bytes()[:2048]
    assert head.startswith(b"ID3")
    assert "Ночной рынок".encode("utf-16-le") in head or b"\xd0" in head


def test_falls_back_to_wav_without_ffmpeg(tmp_path, pieces, monkeypatch):
    monkeypatch.setattr("audiobook.core.assemble.ensure_ffmpeg", lambda: None)
    result = assemble_chapter(pieces, tmp_path / "out.mp3")
    assert result.format == "wav"
    assert result.path.suffix == ".wav"
    assert "ffmpeg" in result.note


def test_duration_is_human_readable(tmp_path, pieces):
    result = assemble_chapter(pieces, tmp_path / "out.wav", normalize=False)
    assert result.duration_str == "0:03"
