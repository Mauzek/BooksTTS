"""Экспорт: m4b с оглавлением, mp3 по главам, обмен разметкой."""

from __future__ import annotations

import json

import pytest
from test_synth import RecordingEngine  # движок-заглушка: не грузит модели

from audiobook import paths
from audiobook.core import catalog, export, repo, synth
from audiobook.core.models import Voice


@pytest.fixture()
def engine(monkeypatch):
    fake = RecordingEngine()
    monkeypatch.setattr(catalog, "engine_for", lambda conn, name: fake)
    monkeypatch.setattr(synth, "BACKOFF", (0.0, 0.0))
    return fake


@pytest.fixture()
def voiced_book(conn, marked, library, engine):
    """Книга с одной озвученной и склеенной главой."""
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar", display_name="aidar"))
    for speaker in repo.book_speakers(conn, marked.book_id):
        repo.set_cast(conn, marked.book_id, speaker, voice.id)
    synth.synthesize_chapter(conn, marked.id)
    synth.assemble_chapter(conn, marked.id)
    return marked


# --------------------------------------------------------------------------
# m4b и mp3
# --------------------------------------------------------------------------


def test_m4b_is_built_with_chapters(conn, voiced_book):
    result = export.export_m4b(conn, voiced_book.book_id)
    path = paths.absolute(result["path"])

    assert path.suffix == ".m4b" and path.stat().st_size > 1000
    # Признак контейнера MP4: box ftyp в начале файла.
    assert path.read_bytes()[4:8] == b"ftyp"
    assert result["chapters"] == 1 and result["duration_ms"] > 0
    # Временные файлы сборки за собой убираем.
    assert not (path.parent / ".build").exists()


def test_m4b_metadata_lists_every_chapter(conn, voiced_book):
    book = repo.get_book(conn, voiced_book.book_id)
    second = repo.create_chapter(conn, book.id, 2, "Вторая", "Текст второй главы.")
    repo.update_chapter(conn, second.id, audio_path=repo.get_chapter(conn, voiced_book.id).audio_path,
                        audio_hash="x", duration_ms=5000)

    chapters = [c for c in repo.list_chapters(conn, book.id) if c.audio_path]
    metadata = export._ffmetadata(book, chapters)
    assert metadata.count("[CHAPTER]") == 2
    assert "title=Глава 2. Вторая" in metadata
    # Главы идут подряд: конец первой — начало второй.
    starts = [int(line.split("=")[1]) for line in metadata.splitlines() if line.startswith("START=")]
    ends = [int(line.split("=")[1]) for line in metadata.splitlines() if line.startswith("END=")]
    assert starts == [0, ends[0]]


def test_export_without_audio_says_so(conn, marked, library):
    with pytest.raises(export.ExportError, match="нет озвученных глав"):
        export.export_m4b(conn, marked.book_id)


def test_mp3_files_are_named_by_chapter(conn, voiced_book):
    result = export.export_mp3(conn, voiced_book.book_id)
    names = [paths.absolute(p).name for p in result["files"]]
    assert names == ["001. Ночной рынок.mp3"]
    assert paths.absolute(result["files"][0]).stat().st_size > 0


def test_windows_forbidden_characters_are_removed():
    assert export.safe_name('Глава 1: "Ночь" / часть?') == "Глава 1 Ночь часть"
    assert export.safe_name("") == "книга"


# --------------------------------------------------------------------------
# Разметка в JSON
# --------------------------------------------------------------------------


def test_markup_round_trip_restores_speakers(conn, marked, library, engine):
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="baya", display_name="baya"))
    repo.set_cast(conn, marked.book_id, "Аглая", voice.id, rate=1.1)
    repo.set_pronunciation(conn, marked.book_id, "колокол", "ко́локол")

    payload = export.markup_payload(conn, marked.book_id)
    before = [(s.speaker, s.text) for s in repo.list_segments(conn, marked.id)]

    # Разметку стёрли — накладываем из файла.
    repo.replace_segments(conn, marked.id, [])
    conn.execute('DELETE FROM "cast"')
    result = export.import_markup(conn, marked.book_id, json.loads(json.dumps(payload)))

    assert [(s.speaker, s.text) for s in repo.list_segments(conn, marked.id)] == before
    assert result["cast"] == 1 and result["lost_segments"] == 0
    assert repo.get_cast(conn, marked.book_id, "Аглая").rate == 1.1
    assert repo.list_pronunciations(conn, marked.book_id)[0]["term"] == "колокол"


def test_markup_survives_small_text_edits(conn, marked, library):
    payload = export.markup_payload(conn, marked.book_id)
    # Опечатку в начале главы поправили: смещения сдвинулись.
    chapter = repo.get_chapter(conn, marked.id)
    repo.update_chapter(conn, marked.id, text=chapter.text.replace("Дождь", "Дождик", 1))
    repo.replace_segments(conn, marked.id, [])

    result = export.import_markup(conn, marked.book_id, payload)
    segments = repo.list_segments(conn, marked.id)
    text = repo.get_chapter(conn, marked.id).text
    assert result["lost_segments"] == 1  # первая реплика изменилась и не нашлась
    assert segments and all(text[s.char_start : s.char_end] == s.text for s in segments)
    assert "Аглая" in {s.speaker for s in segments}


def test_markup_of_another_program_is_refused(conn, marked, library):
    with pytest.raises(export.ExportError, match="не файл разметки"):
        export.import_markup(conn, marked.book_id, {"format": "что-то другое"})


def test_newer_markup_version_is_refused(conn, marked, library):
    payload = export.markup_payload(conn, marked.book_id)
    payload["version"] = export.MARKUP_VERSION + 1
    with pytest.raises(export.ExportError, match="более новой"):
        export.import_markup(conn, marked.book_id, payload)


def test_missing_voice_is_reported_not_silently_dropped(conn, marked, library):
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="xenia", display_name="xenia"))
    repo.set_cast(conn, marked.book_id, "Велимир", voice.id)
    payload = export.markup_payload(conn, marked.book_id)
    conn.execute("DELETE FROM voice")

    result = export.import_markup(conn, marked.book_id, payload)
    assert result["cast"] == 0
    assert result["cast_missing"] == ["Велимир: silero/xenia"]
