"""Тесты слоя данных: схема, CRUD, идемпотентность озвучки."""

from __future__ import annotations

import pytest

from audiobook.core import repo
from audiobook.core.models import Segment, Voice, segment_hash


# --------------------------------------------------------------------------
# Схема
# --------------------------------------------------------------------------


def test_all_tables_exist(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"folder", "book", "chapter", "segment", "voice", "cast", "job"} <= names


def test_foreign_keys_are_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_deleting_a_book_takes_its_chapters(conn, chapter):
    repo.delete_book(conn, chapter.book_id)
    assert conn.execute("SELECT COUNT(*) FROM chapter").fetchone()[0] == 0


def test_deleting_a_chapter_takes_its_segments(conn, marked):
    conn.execute("DELETE FROM chapter WHERE id = ?", (marked.id,))
    assert conn.execute("SELECT COUNT(*) FROM segment").fetchone()[0] == 0


# --------------------------------------------------------------------------
# Папки и книги
# --------------------------------------------------------------------------


def test_folders_nest(conn):
    parent = repo.create_folder(conn, "Фантастика")
    child = repo.create_folder(conn, "Циклы", parent_id=parent.id)
    assert [f.name for f in repo.list_folders(conn, parent.id)] == ["Циклы"]
    assert child.parent_id == parent.id


def test_folder_tree_carries_books(conn):
    folder = repo.create_folder(conn, "Фантастика")
    repo.create_book(conn, title="Книга", folder_id=folder.id)
    tree = repo.folder_tree(conn)
    assert tree[0]["books"][0]["title"] == "Книга"


def test_missing_book_is_a_clear_error(conn):
    with pytest.raises(repo.RepoError, match="книги 999 нет"):
        repo.get_book(conn, 999)


def test_chapter_numbers_are_unique_per_book(conn, chapter):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_chapter(conn, book_id=chapter.book_id, number=1)


# --------------------------------------------------------------------------
# Сегменты
# --------------------------------------------------------------------------


def test_replace_segments_renumbers_from_zero(conn, marked):
    segments = repo.list_segments(conn, marked.id)
    assert [s.order for s in segments] == list(range(len(segments)))


def test_replace_segments_wipes_the_old_ones(conn, marked):
    repo.replace_segments(conn, marked.id, [Segment(speaker="narrator", text="Один.")])
    assert len(repo.list_segments(conn, marked.id)) == 1


def test_speakers_are_counted_by_frequency(conn, marked):
    counts = repo.chapter_speakers(conn, marked.id)
    assert list(counts)[0] == "narrator"  # самый частый идёт первым
    assert counts["Аглая"] == 1


def test_book_speakers_span_all_chapters(conn, marked):
    other = repo.create_chapter(conn, book_id=marked.book_id, number=2, text="текст")
    repo.replace_segments(conn, other.id, [Segment(speaker="Терех", text="Поздно.")])
    assert "Терех" in repo.book_speakers(conn, marked.book_id)


def test_manual_flag_survives_a_round_trip(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    repo.update_segment(conn, first.id, is_manual=True)
    assert repo.get_segment(conn, first.id).is_manual is True


def test_renumber_closes_gaps(conn, marked):
    segments = repo.list_segments(conn, marked.id)
    repo.delete_segment(conn, segments[1].id)
    repo.renumber_segments(conn, marked.id)
    assert [s.order for s in repo.list_segments(conn, marked.id)] == [0, 1, 2, 3]


# --------------------------------------------------------------------------
# Идемпотентность озвучки
# --------------------------------------------------------------------------


def test_segment_without_audio_needs_synthesis():
    assert Segment(text="Реплика").needs_synthesis("silero:aidar") is True


def test_unchanged_segment_does_not_need_synthesis():
    segment = Segment(text="Реплика", audio_path="cache/a.wav")
    segment.audio_hash = segment_hash(segment.text, "silero:aidar")
    assert segment.needs_synthesis("silero:aidar") is False


def test_changed_text_needs_synthesis():
    segment = Segment(text="Реплика", audio_path="cache/a.wav")
    segment.audio_hash = segment_hash(segment.text, "silero:aidar")
    segment.text = "Другая реплика"
    assert segment.needs_synthesis("silero:aidar") is True


def test_changed_voice_needs_synthesis():
    segment = Segment(text="Реплика", audio_path="cache/a.wav")
    segment.audio_hash = segment_hash(segment.text, "silero:aidar")
    assert segment.needs_synthesis("silero:baya") is True


# --------------------------------------------------------------------------
# Голоса и роли
# --------------------------------------------------------------------------


def test_voice_upsert_is_idempotent(conn):
    voice = Voice(engine="silero", voice_key="aidar", display_name="Айдар")
    first = repo.upsert_voice(conn, voice)
    second = repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar",
                                           display_name="Айдар (обновлён)"))
    assert first.id == second.id
    assert second.display_name == "Айдар (обновлён)"


def test_voices_filter_by_engine_and_gender(conn):
    repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar", gender="male"))
    repo.upsert_voice(conn, Voice(engine="silero", voice_key="baya", gender="female"))
    repo.upsert_voice(conn, Voice(engine="elevenlabs", voice_key="xyz", gender="male"))
    assert len(repo.list_voices(conn, engine="silero")) == 2
    assert len(repo.list_voices(conn, gender="male")) == 2


def test_cast_maps_speaker_to_voice(conn, marked):
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="eugene"))
    repo.set_cast(conn, marked.book_id, "Велимир", voice.id, rate=1.05)
    entry = repo.get_cast(conn, marked.book_id, "Велимир")
    assert entry.voice.voice_key == "eugene"
    assert entry.rate == 1.05


def test_cast_reassignment_replaces_not_duplicates(conn, marked):
    first = repo.upsert_voice(conn, Voice(engine="silero", voice_key="eugene"))
    second = repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar"))
    repo.set_cast(conn, marked.book_id, "Велимир", first.id)
    repo.set_cast(conn, marked.book_id, "Велимир", second.id)
    assert len(repo.cast_map(conn, marked.book_id)) == 1
    assert repo.get_cast(conn, marked.book_id, "Велимир").voice_id == second.id


def test_voice_settings_change_the_hash_key(conn, marked):
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="eugene"))
    repo.set_cast(conn, marked.book_id, "Велимир", voice.id, rate=1.0)
    slow = repo.get_cast(conn, marked.book_id, "Велимир").voice_key
    repo.set_cast(conn, marked.book_id, "Велимир", voice.id, rate=1.3)
    assert repo.get_cast(conn, marked.book_id, "Велимир").voice_key != slow


# --------------------------------------------------------------------------
# Задачи
# --------------------------------------------------------------------------


def test_job_progress_follows_done(conn):
    job = repo.create_job(conn, kind="synthesis", total=4)
    assert repo.update_job(conn, job.id, done=2).progress == 0.5


def test_active_jobs_exclude_finished(conn):
    repo.create_job(conn, kind="synthesis")
    finished = repo.create_job(conn, kind="synthesis")
    repo.update_job(conn, finished.id, status="done")
    assert len(repo.list_jobs(conn, active_only=True)) == 1
