"""Пол персонажа по тексту книги и подбор голоса того же пола."""

from __future__ import annotations

import pytest

from audiobook.core import catalog, repo
from audiobook.core.gender import FEMALE, MALE, evidence, guess_gender
from audiobook.core.models import Voice

# Фрагменты из настоящей главы, на которой автоподбор ошибся: Тереху достался
# женский голос, потому что пол персонажей тогда не учитывался.
CHAPTER = (
    "— Поздно, — сказал дед Терех. — Я думал, вы не придёте.\n\n"
    "— Мы пришли, — ответил Велимир.\n\n"
    "— Знаю, — ответила Аглая. — Поэтому и пришла.\n\n"
    "Велимир перевёл взгляд с одного на другого.\n\n"
    "Аглая достала из-под плаща узкий свёрток. Терех не притронулся.\n\n"
    "— А ты сомневался? — Аглая усмехнулась."
)


@pytest.mark.parametrize("name, expected", [
    ("Терех", MALE), ("Велимир", MALE), ("Аглая", FEMALE),
])
def test_gender_comes_from_verbs_next_to_the_name(name, expected):
    assert guess_gender(name, [CHAPTER]) == expected
    assert evidence(name, [CHAPTER])["source"] == "text"


def test_neighbouring_sentence_does_not_vote():
    """«…и пришла. Велимир перевёл» — «пришла» относится к Аглае."""
    votes = evidence("Велимир", ["Поэтому и пришла. Велимир перевёл взгляд."])
    assert votes["female"] == 0 and votes["male"] == 1


def test_majority_outweighs_a_stray_noun():
    # Одно ошибочное «кивнула» против трёх мужских форм.
    text = "Стол Терех. Сказал Терех. Ответил Терех. Кивнула Терех."
    assert guess_gender("Терех", [text]) == MALE


@pytest.mark.parametrize("name, expected", [
    ("Марфа", FEMALE), ("Никита", MALE), ("Илья", MALE), ("Ратибор", MALE),
    ("Любовь", FEMALE), ("Игорь", ""), ("Старуха", FEMALE), ("Дед Мороз", MALE),
])
def test_name_ending_is_the_fallback(name, expected):
    assert guess_gender(name, ["В тексте этого имени нет."]) == expected


def test_auto_assign_matches_character_gender(conn, library):
    book = repo.create_book(conn, "Проверка пола")
    chapter = repo.create_chapter(conn, book.id, 1, "", CHAPTER)
    repo.replace_segments(conn, chapter.id, [
        repo.Segment(speaker="narrator", text="Текст"),
        repo.Segment(speaker="Терех", text="Поздно"),
        repo.Segment(speaker="Велимир", text="Мы пришли"),
        repo.Segment(speaker="Аглая", text="Знаю"),
    ])
    for key, gender in (("aidar", MALE), ("eugene", MALE), ("baya", FEMALE),
                        ("kseniya", FEMALE), ("xenia", FEMALE)):
        repo.upsert_voice(conn, Voice(engine="silero", voice_key=key, display_name=key, gender=gender))

    assigned = {item["speaker"]: item for item in catalog.auto_assign(conn, book.id)}
    assert assigned["narrator"]["voice"]["voice_key"] == "aidar"
    assert assigned["Терех"]["voice"]["gender"] == MALE
    assert assigned["Велимир"]["voice"]["gender"] == MALE
    assert assigned["Аглая"]["voice"]["gender"] == FEMALE

    # Мужских голосов кроме рассказчика один — Терех и Велимир делят его,
    # но звучат на разной высоте.
    terekh, velimir = assigned["Терех"], assigned["Велимир"]
    assert terekh["voice"]["voice_key"] == velimir["voice"]["voice_key"] == "eugene"
    assert terekh["pitch"] != velimir["pitch"]
    assert terekh["shared"] or velimir["shared"]


def test_kept_roles_count_when_sharing_voices(conn, library):
    """Новая роль не должна совпасть один в один с уже назначенной вручную."""
    book = repo.create_book(conn, "Досборка")
    chapter = repo.create_chapter(conn, book.id, 1, "", CHAPTER)
    repo.replace_segments(conn, chapter.id, [
        repo.Segment(speaker="Велимир", text="Мы пришли"),
        repo.Segment(speaker="Терех", text="Поздно"),
    ])
    eugene = repo.upsert_voice(conn, Voice(engine="silero", voice_key="eugene", display_name="eugene", gender=MALE))
    repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar", display_name="aidar", gender=MALE))
    repo.set_cast(conn, book.id, "Велимир", eugene.id)

    added = {item["speaker"]: item for item in catalog.auto_assign(conn, book.id)}
    assert set(added) == {"Терех"}
    assert added["Терех"]["pitch"] != 1.0


def test_cast_view_shows_the_guessed_gender(conn, marked):
    speakers = {s["name"]: s for s in catalog.cast_view(conn, marked.book_id)["speakers"]}
    # В тестовой главе «сказал он» стоит без имени, поэтому Велимир угадан по окончанию.
    assert speakers["Велимир"]["gender"] == MALE
    assert speakers["Аглая"]["gender"] == FEMALE
    assert speakers["narrator"]["gender"] == ""
