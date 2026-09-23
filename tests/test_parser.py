"""Тесты разбора книги. API не трогают — работают офлайн."""

from __future__ import annotations

import pytest

from audiobook.core.parser import (
    ParserError,
    load_book,
    normalize_text,
    parse_chapter_range,
)

BLANK_SEPARATED = """\
Аннотация, которую не надо считать главой.

Глава 1. Первая

Первый абзац первой главы, достаточно длинный, чтобы не считаться мусором.

— Реплика, — сказал кто-то.

ГЛАВА II

Абзац второй главы.

Глава третья: Название

Абзац третьей главы.
"""

LINE_PER_PARAGRAPH = """\
Глава 1
Строка раз.
Строка два.
Глава 2
Строка три.
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_txt_chapters_and_numbering(tmp_path):
    book = load_book(_write(tmp_path, "b.txt", BLANK_SEPARATED), min_chapter_chars=300)
    assert book.chapter_numbers() == [1, 2, 3]
    assert book.chapter(1).title == "Первая"
    assert book.chapter(3).title == "Название"  # «Глава третья» — словом
    assert len(book.chapter(1).paragraphs) == 2


def test_txt_front_matter_dropped(tmp_path):
    book = load_book(_write(tmp_path, "b.txt", BLANK_SEPARATED), min_chapter_chars=300)
    first = book.chapter(1).paragraphs[0].text
    assert "Аннотация" not in first


def test_txt_line_per_paragraph(tmp_path):
    book = load_book(_write(tmp_path, "b.txt", LINE_PER_PARAGRAPH), min_chapter_chars=0)
    assert [len(ch.paragraphs) for ch in book.chapters] == [2, 1]


def test_cp1251_is_read(tmp_path):
    path = tmp_path / "win.txt"
    path.write_bytes(BLANK_SEPARATED.encode("cp1251"))
    book = load_book(path, min_chapter_chars=300)
    assert book.chapter(1).title == "Первая"


def test_paragraph_indexes_are_dense(tmp_path):
    book = load_book(_write(tmp_path, "b.txt", BLANK_SEPARATED), min_chapter_chars=300)
    for chapter in book.chapters:
        assert [p.index for p in chapter.paragraphs] == list(range(len(chapter.paragraphs)))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("- Привет, - сказал он.", "— Привет, — сказал он."),
        ("– Привет", "— Привет"),
        ("сло­во   с   пробелами", "слово с пробелами"),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("1-5", [1, 2, 3, 4, 5]),
        ("3", [3]),
        ("1-3,7,9-10", [1, 2, 3, 7, 9, 10]),
        ("5-3", [3, 4, 5]),
        ("2,2,2", [2]),
    ],
)
def test_parse_chapter_range(spec, expected):
    assert parse_chapter_range(spec) == expected


def test_parse_chapter_range_rejects_garbage():
    with pytest.raises(ParserError):
        parse_chapter_range("первая-пятая")


def test_unknown_format(tmp_path):
    path = tmp_path / "book.djvu"
    path.write_bytes(b"%PDF")
    with pytest.raises(ParserError, match="неподдерживаемый формат"):
        load_book(path)


def test_missing_file(tmp_path):
    with pytest.raises(ParserError, match="не найден"):
        load_book(tmp_path / "нет.txt")
