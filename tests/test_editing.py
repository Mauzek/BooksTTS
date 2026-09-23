"""Тесты операций редактора и журнала отмены."""

from __future__ import annotations

import pytest

from audiobook.core import editing, repo
from audiobook.core.models import Segment, segment_hash

SESSION = "тестовая-сессия"


def texts(conn, chapter_id) -> list[str]:
    return [s.text for s in repo.list_segments(conn, chapter_id)]


def speakers(conn, chapter_id) -> list[str]:
    return [s.speaker for s in repo.list_segments(conn, chapter_id)]


# --------------------------------------------------------------------------
# Назначение говорящего
# --------------------------------------------------------------------------


def test_assign_sets_speaker_and_manual_flag(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Аглая")
    updated = repo.get_segment(conn, first.id)
    assert updated.speaker == "Аглая"
    assert updated.is_manual is True


def test_assign_accepts_several_segments_at_once(conn, marked):
    ids = [s.id for s in repo.list_segments(conn, marked.id)[:3]]
    editing.assign_speaker(conn, SESSION, marked.id, ids, "Терех")
    assert speakers(conn, marked.id)[:3] == ["Терех"] * 3


def test_assign_without_selection_is_an_error(conn, marked):
    with pytest.raises(editing.EditError, match="не выбрано"):
        editing.assign_speaker(conn, SESSION, marked.id, [], "Аглая")


def test_empty_speaker_falls_back_to_narrator(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "  ")
    assert repo.get_segment(conn, first.id).speaker == "narrator"


# --------------------------------------------------------------------------
# Выделение мышью
# --------------------------------------------------------------------------


def test_range_inside_one_segment_splits_it(conn, marked):
    segments = repo.list_segments(conn, marked.id)
    target = segments[0]
    # Берём середину первого сегмента.
    start = target.char_start + 6
    end = target.char_start + 18
    editing.assign_range(conn, SESSION, marked.id, start, end, "Аглая")

    result = repo.list_segments(conn, marked.id)
    assert len(result) == len(segments) + 2  # до, выделение, после
    assert result[1].speaker == "Аглая"
    assert "".join(s.text for s in result[:3]) == target.text


def test_range_covering_whole_segment_keeps_it_whole(conn, marked):
    target = repo.list_segments(conn, marked.id)[1]
    editing.assign_range(
        conn, SESSION, marked.id, target.char_start, target.char_end, "Терех"
    )
    result = repo.list_segments(conn, marked.id)
    assert [s.text for s in result if s.speaker == "Терех"] == [target.text]


def test_range_across_two_segments_touches_both(conn, marked):
    segments = repo.list_segments(conn, marked.id)
    start = segments[1].char_start
    end = segments[2].char_end
    editing.assign_range(conn, SESSION, marked.id, start, end, "Аглая")
    assigned = [s for s in repo.list_segments(conn, marked.id) if s.speaker == "Аглая"]
    assert len(assigned) >= 2


def test_range_never_loses_text(conn, marked):
    before = "".join(texts(conn, marked.id))
    segments = repo.list_segments(conn, marked.id)
    editing.assign_range(
        conn, SESSION, marked.id,
        segments[0].char_start + 5, segments[1].char_start + 4, "Аглая",
    )
    assert "".join(texts(conn, marked.id)) == before


def test_empty_range_is_an_error(conn, marked):
    with pytest.raises(editing.EditError, match="пустое выделение"):
        editing.assign_range(conn, SESSION, marked.id, 10, 10, "Аглая")


def test_range_outside_any_segment_is_an_error(conn, marked):
    with pytest.raises(editing.EditError, match="не попало"):
        editing.assign_range(conn, SESSION, marked.id, 99000, 99100, "Аглая")


def test_range_drops_stale_audio(conn, marked):
    segments = repo.list_segments(conn, marked.id)
    target = segments[0]
    repo.update_segment(
        conn, target.id,
        audio_path="cache/a.wav",
        audio_hash=segment_hash(target.text, "silero:aidar"),
    )
    editing.assign_range(
        conn, SESSION, marked.id, target.char_start + 3, target.char_start + 10, "Аглая"
    )
    changed = [s for s in repo.list_segments(conn, marked.id) if s.speaker == "Аглая"]
    assert all(s.audio_path is None for s in changed)


# --------------------------------------------------------------------------
# Разделение и склейка
# --------------------------------------------------------------------------


def test_split_makes_two_segments(conn, marked):
    target = repo.list_segments(conn, marked.id)[0]
    editing.split_segment(conn, SESSION, marked.id, target.id, 10)
    result = repo.list_segments(conn, marked.id)
    assert result[0].text == target.text[:10]
    assert result[1].text == target.text[10:]
    assert result[0].speaker == result[1].speaker == target.speaker


def test_split_marks_both_halves_manual(conn, marked):
    target = repo.list_segments(conn, marked.id)[0]
    editing.split_segment(conn, SESSION, marked.id, target.id, 10)
    assert all(s.is_manual for s in repo.list_segments(conn, marked.id)[:2])


@pytest.mark.parametrize("offset", [0, 10_000])
def test_split_outside_the_text_is_an_error(conn, marked, offset):
    target = repo.list_segments(conn, marked.id)[0]
    with pytest.raises(editing.EditError, match="нечего делить"):
        editing.split_segment(conn, SESSION, marked.id, target.id, offset)


def test_merge_joins_neighbours_of_one_speaker(conn, marked):
    target = repo.list_segments(conn, marked.id)[0]
    editing.split_segment(conn, SESSION, marked.id, target.id, 10)
    halves = [s.id for s in repo.list_segments(conn, marked.id)[:2]]
    editing.merge_segments(conn, SESSION, marked.id, halves)
    assert repo.list_segments(conn, marked.id)[0].text == target.text


def test_merge_refuses_different_speakers(conn, marked):
    ids = [s.id for s in repo.list_segments(conn, marked.id)[:2]]
    with pytest.raises(editing.EditError, match="разные говорящие"):
        editing.merge_segments(conn, SESSION, marked.id, ids)


def test_merge_refuses_non_adjacent(conn, marked):
    segments = repo.list_segments(conn, marked.id)
    narrators = [s.id for s in segments if s.speaker == "narrator"]
    with pytest.raises(editing.EditError, match="соседние"):
        editing.merge_segments(conn, SESSION, marked.id, narrators[:2])


def test_merge_needs_at_least_two(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    with pytest.raises(editing.EditError, match="хотя бы два"):
        editing.merge_segments(conn, SESSION, marked.id, [first.id])


# --------------------------------------------------------------------------
# Массовое переименование
# --------------------------------------------------------------------------


def test_rename_replaces_every_mention_in_chapter(conn, marked):
    editing.rename_speaker(conn, SESSION, marked.id, "Велимир", "Веля")
    assert "Веля" in speakers(conn, marked.id)
    assert "Велимир" not in speakers(conn, marked.id)


def test_rename_across_the_book(conn, marked):
    other = repo.create_chapter(conn, book_id=marked.book_id, number=2, text="текст")
    repo.replace_segments(conn, other.id, [Segment(speaker="Велимир", text="Внук.")])
    editing.rename_speaker(conn, SESSION, marked.id, "Велимир", "Веля", whole_book=True)
    assert speakers(conn, other.id) == ["Веля"]


def test_rename_merges_into_an_existing_speaker(conn, marked):
    editing.rename_speaker(conn, SESSION, marked.id, "Велимир", "Аглая")
    counts = repo.chapter_speakers(conn, marked.id)
    assert counts["Аглая"] == 2
    assert "Велимир" not in counts


def test_rename_of_unknown_speaker_is_an_error(conn, marked):
    with pytest.raises(editing.EditError, match="не нашлось"):
        editing.rename_speaker(conn, SESSION, marked.id, "Никто", "Кто-то")


def test_rename_to_the_same_name_is_an_error(conn, marked):
    with pytest.raises(editing.EditError, match="совпадают"):
        editing.rename_speaker(conn, SESSION, marked.id, "Аглая", "Аглая")


# --------------------------------------------------------------------------
# Текст и эмоция
# --------------------------------------------------------------------------


def test_text_edit_drops_the_audio(conn, marked):
    target = repo.list_segments(conn, marked.id)[0]
    repo.update_segment(conn, target.id, audio_path="cache/a.wav", audio_hash="abc")
    editing.set_text(conn, SESSION, marked.id, target.id, "Совсем другой текст.")
    updated = repo.get_segment(conn, target.id)
    assert updated.audio_path is None and updated.audio_hash is None


def test_empty_text_is_refused(conn, marked):
    target = repo.list_segments(conn, marked.id)[0]
    with pytest.raises(editing.EditError, match="не может быть пустым"):
        editing.set_text(conn, SESSION, marked.id, target.id, "   ")


def test_emotion_is_stored(conn, marked):
    target = repo.list_segments(conn, marked.id)[1]
    editing.set_emotion(conn, SESSION, marked.id, [target.id], "зло")
    assert repo.get_segment(conn, target.id).emotion == "зло"


# --------------------------------------------------------------------------
# Отмена и повтор
# --------------------------------------------------------------------------


def test_undo_returns_the_previous_speaker(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Аглая")
    editing.undo(conn, SESSION, marked.id)
    assert speakers(conn, marked.id)[0] == "narrator"


def test_redo_puts_the_change_back(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Аглая")
    editing.undo(conn, SESSION, marked.id)
    editing.redo(conn, SESSION, marked.id)
    assert speakers(conn, marked.id)[0] == "Аглая"


def test_undo_walks_the_whole_history(conn, marked):
    before = speakers(conn, marked.id)
    for speaker in ("Аглая", "Терех", "Веля"):
        first = repo.list_segments(conn, marked.id)[0]
        editing.assign_speaker(conn, SESSION, marked.id, [first.id], speaker)
    for _ in range(3):
        editing.undo(conn, SESSION, marked.id)
    assert speakers(conn, marked.id) == before


def test_undo_restores_structure_not_just_fields(conn, marked):
    before = texts(conn, marked.id)
    target = repo.list_segments(conn, marked.id)[0]
    editing.split_segment(conn, SESSION, marked.id, target.id, 10)
    editing.undo(conn, SESSION, marked.id)
    assert texts(conn, marked.id) == before


def test_new_edit_discards_the_redo_tail(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Аглая")
    editing.undo(conn, SESSION, marked.id)
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Терех")
    with pytest.raises(editing.EditError, match="повторять нечего"):
        editing.redo(conn, SESSION, marked.id)


def test_undo_with_empty_history_is_an_error(conn, marked):
    with pytest.raises(editing.EditError, match="отменять нечего"):
        editing.undo(conn, SESSION, marked.id)


def test_history_is_per_session(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Аглая")
    with pytest.raises(editing.EditError):
        editing.undo(conn, "другая-сессия", marked.id)


def test_history_state_reports_both_directions(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    assert editing.history_state(conn, SESSION, marked.id) == {
        "can_undo": False, "can_redo": False
    }
    editing.assign_speaker(conn, SESSION, marked.id, [first.id], "Аглая")
    assert editing.history_state(conn, SESSION, marked.id)["can_undo"] is True
    editing.undo(conn, SESSION, marked.id)
    assert editing.history_state(conn, SESSION, marked.id)["can_redo"] is True


def test_history_survives_a_new_connection(db, conn, marked):
    """Журнал лежит в базе, а не в памяти процесса."""
    segment = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, SESSION, marked.id, [segment.id], "Аглая")
    conn.commit()  # другое соединение видит только зафиксированное

    with db.connect() as other:
        editing.undo(other, SESSION, marked.id)
        assert speakers(other, marked.id)[0] == "narrator"
