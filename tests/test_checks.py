"""Тесты проверок разметки перед синтезом."""

from __future__ import annotations

from audiobook.core import checks, repo
from audiobook.core.models import Segment, Voice


def kinds(findings) -> set[str]:
    return {f.kind for f in findings}


def by_kind(findings, kind) -> list[checks.Finding]:
    return [f for f in findings if f.kind == kind]


def give_everyone_a_voice(conn, book_id):
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar"))
    for speaker in repo.book_speakers(conn, book_id):
        repo.set_cast(conn, book_id, speaker, voice.id)


# --------------------------------------------------------------------------
# Голоса
# --------------------------------------------------------------------------


def test_unmarked_chapter_is_reported(conn, chapter):
    findings = checks.check_chapter(conn, chapter.id)
    assert kinds(findings) == {"no_markup"}


def test_speakers_without_voices_are_blockers(conn, marked):
    findings = by_kind(checks.check_chapter(conn, marked.id), "no_voice")
    assert {f.speaker for f in findings} == {"narrator", "Велимир", "Аглая"}
    assert all(f.severity == "blocker" for f in findings)


def test_assigned_voices_clear_the_blocker(conn, marked):
    give_everyone_a_voice(conn, marked.book_id)
    assert not by_kind(checks.check_chapter(conn, marked.id), "no_voice")


def test_cast_without_a_voice_still_blocks(conn, marked):
    repo.set_cast(conn, marked.book_id, "Аглая", None)
    blocked = {f.speaker for f in by_kind(checks.check_chapter(conn, marked.id), "no_voice")}
    assert "Аглая" in blocked


def test_no_voice_finding_lists_its_segments(conn, marked):
    finding = by_kind(checks.check_chapter(conn, marked.id), "no_voice")[0]
    assert finding.segment_ids


# --------------------------------------------------------------------------
# Редкие персонажи
# --------------------------------------------------------------------------


def test_one_line_speaker_is_suspicious(conn, marked):
    give_everyone_a_voice(conn, marked.book_id)
    rare = {f.speaker for f in by_kind(checks.check_chapter(conn, marked.id), "rare_speaker")}
    assert "Аглая" in rare  # одна реплика на книгу


def test_frequent_speaker_is_not_reported(conn, marked):
    give_everyone_a_voice(conn, marked.book_id)
    rare = {f.speaker for f in by_kind(checks.check_chapter(conn, marked.id), "rare_speaker")}
    assert "narrator" not in rare


def test_rare_speaker_is_only_a_warning(conn, marked):
    give_everyone_a_voice(conn, marked.book_id)
    assert all(
        f.severity == "warning"
        for f in by_kind(checks.check_chapter(conn, marked.id), "rare_speaker")
    )


def test_speaker_stops_being_rare_across_the_book(conn, marked):
    other = repo.create_chapter(conn, book_id=marked.book_id, number=2, text="текст")
    repo.replace_segments(
        conn, other.id, [Segment(speaker="Аглая", text=f"Реплика {i}.") for i in range(5)]
    )
    give_everyone_a_voice(conn, marked.book_id)
    rare = {f.speaker for f in by_kind(checks.check_chapter(conn, marked.id), "rare_speaker")}
    assert "Аглая" not in rare


# --------------------------------------------------------------------------
# Длинный рассказчик в диалоге
# --------------------------------------------------------------------------


def test_long_narrator_between_replies_is_reported(conn, chapter):
    long_text = "Описание, растянувшееся на много строк. " * 15
    repo.replace_segments(
        conn,
        chapter.id,
        [
            Segment(speaker="Аглая", text="Первая реплика."),
            Segment(speaker="narrator", text=long_text),
            Segment(speaker="Велимир", text="Вторая реплика."),
        ],
    )
    give_everyone_a_voice(conn, chapter.book_id)
    assert by_kind(checks.check_chapter(conn, chapter.id), "long_narrator")


def test_long_narrator_outside_dialogue_is_fine(conn, chapter):
    long_text = "Описание, растянувшееся на много строк. " * 15
    repo.replace_segments(
        conn,
        chapter.id,
        [
            Segment(speaker="narrator", text="Начало."),
            Segment(speaker="narrator", text=long_text),
            Segment(speaker="narrator", text="Конец."),
        ],
    )
    give_everyone_a_voice(conn, chapter.book_id)
    assert not by_kind(checks.check_chapter(conn, chapter.id), "long_narrator")


def test_short_narrator_in_dialogue_is_fine(conn, marked):
    give_everyone_a_voice(conn, marked.book_id)
    assert not by_kind(checks.check_chapter(conn, marked.id), "long_narrator")


# --------------------------------------------------------------------------
# Книга целиком
# --------------------------------------------------------------------------


def test_book_check_covers_every_chapter(conn, marked):
    repo.create_chapter(conn, book_id=marked.book_id, number=2, text="текст")
    assert len(checks.check_book(conn, marked.book_id)) == 2
