"""Тесты авторазметки. Настоящий API не вызывается — клиент подменён."""

from __future__ import annotations

import json

import pytest
from fakes import FakeClient, echo_narrator, targets

from audiobook.core import library, repo
from audiobook.core.markup import (
    BatchFailed,
    CharacterRegistry,
    MarkupParseError,
    build_batches,
    extract_json_array,
    markup_text,
)
from audiobook.core.models import NARRATOR
from audiobook.core.parser import Paragraph


def dialogue(message: str, _call: int) -> str:
    """Первый абзац — рассказчик, реплики в тире — Аглая."""
    items = []
    for number, text in targets(message):
        speaker = "Аглая" if text.startswith("—") else NARRATOR
        items.append(
            {"para": number, "speaker": speaker,
             "text": text.lstrip("— ").strip(), "emotion": "нейтрально"}
        )
    return json.dumps(items, ensure_ascii=False)


# --------------------------------------------------------------------------
# Батчи
# --------------------------------------------------------------------------


def _paras(sizes):
    return [Paragraph(i, "я" * n) for i, n in enumerate(sizes)]


def test_batches_respect_the_limit():
    batches = build_batches(_paras([100] * 10), max_chars=250, overlap=1)
    assert [len(b.paragraphs) for b in batches] == [2, 2, 2, 2, 2]


def test_oversized_paragraph_goes_alone():
    batches = build_batches(_paras([50, 900, 50]), max_chars=100, overlap=1)
    assert len(batches[1].paragraphs) == 1


def test_overlap_is_the_previous_tail():
    batches = build_batches(_paras([100] * 6), max_chars=250, overlap=1)
    assert batches[0].context == []
    assert batches[1].context[0].index == 1


def test_every_paragraph_appears_once():
    batches = build_batches(_paras([70] * 13), max_chars=200, overlap=1)
    seen = [p.index for b in batches for p in b.paragraphs]
    assert seen == list(range(13))


# --------------------------------------------------------------------------
# Разбор ответа
# --------------------------------------------------------------------------


def test_plain_array():
    assert extract_json_array('[{"para": 0}]') == [{"para": 0}]


def test_markdown_fence_is_stripped():
    assert extract_json_array('```json\n[{"para": 1}]\n```')[0]["para"] == 1


def test_prose_around_is_ignored():
    assert extract_json_array('Вот:\n[{"para": 2}]\nГотово.')[0]["para"] == 2


def test_trailing_comma_is_repaired():
    assert extract_json_array('[{"para": 0},]')[0]["para"] == 0


def test_non_json_is_an_error():
    with pytest.raises(MarkupParseError):
        extract_json_array("совсем не json")


# --------------------------------------------------------------------------
# Сведение имён
# --------------------------------------------------------------------------


def test_transliteration_variants_merge():
    registry = CharacterRegistry(known=["Рудеус"])
    assert registry.resolve("Рудэус") == "Рудеус"
    assert registry.names() == ["Рудеус"]


def test_typos_merge():
    assert CharacterRegistry(known=["Велимир"]).resolve("Велемир") == "Велимир"


def test_different_names_stay_apart():
    assert CharacterRegistry(known=["Елена"]).resolve("Алёна") == "Алёна"


def test_short_forms_need_an_explicit_alias():
    assert CharacterRegistry(known=["Рудеус"]).resolve("Руди") == "Руди"
    registry = CharacterRegistry(known=["Рудеус"], aliases={"Руди": "Рудеус"})
    assert registry.resolve("Руди") == "Рудеус"


@pytest.mark.parametrize("raw", ["narrator", "Рассказчик", "автор", "", None])
def test_narrator_synonyms(raw):
    assert CharacterRegistry().resolve(raw) == NARRATOR


# --------------------------------------------------------------------------
# Разметка текста
# --------------------------------------------------------------------------

TEXT = (
    "Дождь кончился час назад.\n\n"
    "— Ты всё-таки пришла.\n\n"
    "Он промолчал."
)


def test_markup_returns_segments_for_every_paragraph():
    result = markup_text(TEXT, client=FakeClient(dialogue))
    assert len(result.segments) == 3
    assert result.ok


def test_offsets_point_into_the_source_text():
    result = markup_text(TEXT, client=FakeClient(dialogue))
    for segment in result.segments:
        assert segment.char_start is not None
        assert TEXT[segment.char_start : segment.char_end] == segment.text


def test_segments_come_back_in_reading_order():
    result = markup_text(TEXT, client=FakeClient(dialogue), batch_chars=40)
    starts = [s.char_start for s in result.segments]
    assert starts == sorted(starts)


def test_characters_are_collected():
    result = markup_text(TEXT, client=FakeClient(dialogue))
    assert result.characters == ["Аглая"]


def test_known_names_reach_the_prompt():
    client = FakeClient(dialogue)
    markup_text(TEXT, client=client, known=["Велимир"])
    assert "Велимир" in client.calls[0]["messages"][0]["content"]


def test_skipped_paragraphs_are_not_sent():
    client = FakeClient(dialogue)
    markup_text(TEXT, client=client, skip_paragraphs=[1])
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Ты всё-таки пришла" not in prompt.split("АБЗАЦЫ")[1]


def test_summarized_paragraph_falls_back_to_the_source():
    def lazy(message, _call):
        return json.dumps(
            [{"para": n, "speaker": "Аглая", "text": "кратко"}
             for n, _ in targets(message)],
            ensure_ascii=False,
        )

    result = markup_text(TEXT, client=FakeClient(lazy))
    assert any(i.kind == "coverage" for i in result.issues)
    assert result.segments[0].text == "Дождь кончился час назад."


def test_missing_paragraph_is_recovered():
    def partial(message, _call):
        found = targets(message)
        return json.dumps(
            [{"para": found[0][0], "speaker": "narrator", "text": found[0][1]}],
            ensure_ascii=False,
        )

    result = markup_text(TEXT, client=FakeClient(partial), batch_chars=10_000)
    assert any(i.kind == "uncovered" for i in result.issues)
    assert len(result.segments) == 3  # ничего не потеряно


def test_failed_batch_keeps_the_text(monkeypatch):
    monkeypatch.setattr("audiobook.core.markup.time.sleep", lambda _s: None)
    result = markup_text(TEXT, client=FakeClient(lambda m, c: "не json"), attempts=2)
    assert not result.ok
    assert all(s.speaker == NARRATOR for s in result.segments)
    assert all(s.error for s in result.segments)


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr("audiobook.core.markup.time.sleep", lambda _s: None)
    client = FakeClient(lambda m, c: "сломалось" if c == 1 else dialogue(m, c))
    result = markup_text(TEXT, client=client, attempts=3)
    assert result.ok
    assert len(client.calls) == 2


# --------------------------------------------------------------------------
# Разметка главы в базе
# --------------------------------------------------------------------------


def test_markup_chapter_stores_segments(conn, chapter):
    library.markup_chapter(conn, chapter.id, client=FakeClient(dialogue))
    assert len(repo.list_segments(conn, chapter.id)) == 4


def test_manual_segments_survive_a_second_markup(conn, chapter):
    library.markup_chapter(conn, chapter.id, client=FakeClient(dialogue))
    target = repo.list_segments(conn, chapter.id)[1]
    repo.update_segment(conn, target.id, speaker="Велимир", is_manual=True)

    library.markup_chapter(conn, chapter.id, client=FakeClient(echo_narrator))
    speakers = [s.speaker for s in repo.list_segments(conn, chapter.id)]
    assert "Велимир" in speakers  # ручная правка на месте


def test_manual_paragraph_is_not_sent_to_the_model(conn, chapter):
    library.markup_chapter(conn, chapter.id, client=FakeClient(dialogue))
    target = repo.list_segments(conn, chapter.id)[1]
    repo.update_segment(conn, target.id, is_manual=True)

    client = FakeClient(dialogue)
    library.markup_chapter(conn, chapter.id, client=client)
    sent = " ".join(call["messages"][0]["content"].split("АБЗАЦЫ")[1] for call in client.calls)
    assert target.text not in sent


def test_force_overwrites_manual_edits(conn, chapter):
    library.markup_chapter(conn, chapter.id, client=FakeClient(dialogue))
    target = repo.list_segments(conn, chapter.id)[1]
    repo.update_segment(conn, target.id, speaker="Велимир", is_manual=True)

    library.markup_chapter(conn, chapter.id, client=FakeClient(echo_narrator), force=True)
    assert all(s.speaker == NARRATOR for s in repo.list_segments(conn, chapter.id))


def test_known_speakers_of_the_book_are_passed_on(conn, chapter):
    other = repo.create_chapter(conn, book_id=chapter.book_id, number=2, text="текст")
    from audiobook.core.models import Segment

    repo.replace_segments(conn, other.id, [Segment(speaker="Терех", text="Поздно.")])

    client = FakeClient(dialogue)
    library.markup_chapter(conn, chapter.id, client=client)
    assert "Терех" in client.calls[0]["messages"][0]["content"]
