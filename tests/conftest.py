"""Общие фикстуры: временная библиотека, база и книга с главой."""

from __future__ import annotations

import pytest

from audiobook import paths
from audiobook.core import repo
from audiobook.core.db import Database
from audiobook.core.models import Segment
from audiobook.core.parser import chapter_text

CHAPTER_PARAGRAPHS = [
    "Дождь кончился час назад, но крыши всё ещё роняли воду.",
    "— Ты всё-таки пришла, — сказал он.",
    "— А ты сомневался? Я сказала, что приду.",
    "Он промолчал. Где-то за домами ударил колокол.",
]


@pytest.fixture()
def library(tmp_path, monkeypatch):
    """Временный корень библиотеки — ни один тест не трогает настоящий."""
    root = tmp_path / "library"
    paths.set_library_root(root)
    paths.ensure_layout()
    yield root
    paths.set_library_root(None)


@pytest.fixture()
def db(library):
    return Database(library / "test.db").setup()


@pytest.fixture()
def conn(db):
    with db.connect() as connection:
        yield connection


@pytest.fixture()
def chapter(conn):
    """Книга с одной главой из четырёх абзацев, без разметки."""
    book = repo.create_book(conn, title="Проба", author="Автор")
    return repo.create_chapter(
        conn,
        book_id=book.id,
        number=1,
        title="Ночной рынок",
        text=chapter_text(CHAPTER_PARAGRAPHS),
    )


def make_segments(text: str, spec: list[tuple[str, str]]) -> list[Segment]:
    """Сегменты со смещениями, найденными в тексте главы."""
    segments: list[Segment] = []
    cursor = 0
    for speaker, fragment in spec:
        start = text.index(fragment, cursor)
        segments.append(
            Segment(
                speaker=speaker,
                text=fragment,
                char_start=start,
                char_end=start + len(fragment),
            )
        )
        cursor = start + len(fragment)
    return segments


@pytest.fixture()
def marked(conn, chapter):
    """Та же глава, но уже размеченная: рассказчик и два персонажа."""
    segments = make_segments(
        chapter.text,
        [
            ("narrator", "Дождь кончился час назад, но крыши всё ещё роняли воду."),
            ("Велимир", "Ты всё-таки пришла"),
            ("narrator", "сказал он."),
            ("Аглая", "А ты сомневался? Я сказала, что приду."),
            ("narrator", "Он промолчал. Где-то за домами ударил колокол."),
        ],
    )
    repo.replace_segments(conn, chapter.id, segments)
    return chapter
