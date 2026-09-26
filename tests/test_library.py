"""Библиотека: миграция схемы, дерево папок, границы глав, поиск, форматы."""

from __future__ import annotations

import sqlite3
import zipfile

import pytest

from audiobook import paths
from audiobook.core import db as db_mod
from audiobook.core import editing, library, repo
from audiobook.core.formats import (
    HEADING,
    PARAGRAPH,
    chapters_from_blocks,
    ensure_unique_numbers,
    pdf_blocks,
)
from audiobook.core.parser import Chapter, Paragraph, ParserError, load_book

# --------------------------------------------------------------------------
# Миграция
# --------------------------------------------------------------------------


def test_v1_library_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "старая.db"
    old = sqlite3.connect(path)
    old.executescript(db_mod.SCHEMA + "PRAGMA user_version = 1;")
    old.execute("INSERT INTO book (title) VALUES ('Старая книга')")
    old.execute(
        "INSERT INTO chapter (book_id, number, title, text) VALUES (1, 1, 'Ёлка', 'Жили-были ёжики')"
    )
    old.commit()
    old.close()

    database = db_mod.Database(path).setup()
    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_mod.SCHEMA_VERSION
        assert repo.get_book(conn, 1).title == "Старая книга"
        # Индекс заполнен и для книг, которые были до миграции.
        assert [hit["kind"] for hit in repo.search(conn, "ежики")] == ["chapter"]


def test_v3_dictionary_survives_and_can_hold_shared_rules(tmp_path):
    """Версия 4 пересобирает словарь: старые правила книг не теряются."""
    path = tmp_path / "v3.db"
    old = sqlite3.connect(path)
    old.executescript(db_mod.SCHEMA + "PRAGMA user_version = 1;")
    for target in (2, 3):
        old.executescript(f"BEGIN;\n{db_mod.MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;")
    old.execute("INSERT INTO book (title) VALUES ('Книга')")
    old.execute("INSERT INTO pronunciation (book_id, term, replacement) VALUES (1, 'Терех', 'Т+ерех')")
    old.commit()
    old.close()

    database = db_mod.Database(path).setup()
    with database.connect() as conn:
        assert repo.list_pronunciations(conn, 1)[0]["replacement"] == "Т+ерех"
        repo.set_pronunciation(conn, None, "т. е.", "то есть")
        repo.set_pronunciation(conn, None, "т. е.", "то есть")  # повтор — не дубль
        assert [r["term"] for r in repo.list_pronunciations(conn, None)] == ["т. е."]


def test_v4_progress_becomes_listened_marks(tmp_path):
    """Версия 5: главы до текущей и дослушанная текущая получают галочку."""
    path = tmp_path / "v4.db"
    old = sqlite3.connect(path)
    old.executescript(db_mod.SCHEMA + "PRAGMA user_version = 1;")
    for target in (2, 3, 4):
        old.executescript(f"BEGIN;\n{db_mod.MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;")
    old.execute("INSERT INTO book (title) VALUES ('Книга')")
    for number in (1, 2, 3):
        old.execute(
            "INSERT INTO chapter (book_id, number, audio_path, duration_ms) VALUES (1, ?, ?, 60000)",
            (number, f"audio/{number}.mp3"),
        )
    old.execute("INSERT INTO playback (book_id, chapter_id, position_ms) VALUES (1, 2, 59000)")
    old.commit()
    old.close()

    database = db_mod.Database(path).setup()
    with database.connect() as conn:
        heard = [bool(c.listened_at) for c in repo.list_chapters(conn, 1)]
        assert heard == [True, True, False]
        assert repo.get_book(conn, 1).genres == []


def test_book_rule_beats_the_shared_one(conn, marked):
    repo.set_pronunciation(conn, None, "колокол", "общий")
    repo.set_pronunciation(conn, None, "т. е.", "то есть")
    repo.set_pronunciation(conn, marked.book_id, "Колокол", "книжный")
    rules = {r["term"]: r["replacement"] for r in repo.rules_for_book(conn, marked.book_id)}
    assert rules == {"т. е.": "то есть", "Колокол": "книжный"}


def test_setup_twice_is_harmless(db):
    db.setup()
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_mod.SCHEMA_VERSION


def test_library_from_a_newer_app_is_refused(tmp_path):
    path = tmp_path / "новая.db"
    newer = sqlite3.connect(path)
    newer.execute("PRAGMA user_version = 99")
    newer.close()
    with pytest.raises(RuntimeError, match="более новой"):
        db_mod.Database(path).setup()


# --------------------------------------------------------------------------
# Дерево папок
# --------------------------------------------------------------------------


def test_nested_folders_form_a_tree(conn):
    series = repo.create_folder(conn, "Цикл")
    volume = repo.create_folder(conn, "Том 1", series.id)
    repo.create_book(conn, "Книга", folder_id=volume.id)
    repo.create_book(conn, "Без папки")

    tree = library.library_tree(conn)
    assert tree["folders"][0]["name"] == "Цикл"
    assert tree["folders"][0]["folders"][0]["books"][0]["title"] == "Книга"
    assert [b["title"] for b in tree["books"]] == ["Без папки"]


def test_folder_cannot_be_moved_into_its_own_subfolder(conn):
    parent = repo.create_folder(conn, "Родитель")
    child = repo.create_folder(conn, "Ребёнок", parent.id)
    with pytest.raises(repo.InvalidOperation, match="внутрь"):
        repo.move_folder(conn, parent.id, child.id)
    with pytest.raises(repo.InvalidOperation):
        repo.move_folder(conn, parent.id, parent.id)


def test_folder_moves_to_root(conn):
    parent = repo.create_folder(conn, "Родитель")
    child = repo.create_folder(conn, "Ребёнок", parent.id)
    repo.move_folder(conn, child.id, None)
    assert {f.name for f in repo.list_folders(conn)} == {"Родитель", "Ребёнок"}


def test_deleting_a_folder_keeps_its_books(conn):
    series = repo.create_folder(conn, "Цикл")
    volume = repo.create_folder(conn, "Том", series.id)
    book = repo.create_book(conn, "Книга", folder_id=volume.id)
    repo.delete_folder(conn, series.id)
    assert repo.get_book(conn, book.id).folder_id is None
    assert repo.list_folders(conn) == []


def test_move_book_sets_its_place_among_siblings(conn):
    folder = repo.create_folder(conn, "Полка")
    a, b, c = (repo.create_book(conn, t, folder_id=folder.id) for t in ("А", "Б", "В"))
    repo.move_book(conn, c.id, folder.id, position=0)
    assert [x.title for x in repo.list_books(conn, folder.id)] == ["В", "А", "Б"]


def test_empty_folder_name_is_refused(conn):
    folder = repo.create_folder(conn, "Имя")
    with pytest.raises(repo.InvalidOperation):
        repo.rename_folder(conn, folder.id, "   ")


def test_renaming_updates_the_search_index(conn):
    folder = repo.create_folder(conn, "Архив")
    repo.rename_folder(conn, folder.id, "Фантастика")
    assert [h["ref_id"] for h in repo.search(conn, "фантаст")] == [folder.id]
    assert repo.search(conn, "архив") == []


# --------------------------------------------------------------------------
# Границы глав
# --------------------------------------------------------------------------


def _offsets_match(conn, chapter_id):
    chapter = repo.get_chapter(conn, chapter_id)
    for segment in repo.list_segments(conn, chapter_id):
        assert chapter.text[segment.char_start : segment.char_end] == segment.text


def test_split_moves_text_and_markup(conn, marked):
    boundary = marked.text.index("— А ты сомневался")
    head, tail = library.split_chapter(conn, marked.id, boundary + 5, title="Вторая половина")

    assert head.text.endswith("сказал он.")
    assert tail.text.startswith("— А ты сомневался")
    assert (tail.number, tail.title) == (head.number + 1, "Вторая половина")
    assert [s.speaker for s in repo.list_segments(conn, tail.id)] == ["Аглая", "narrator"]
    _offsets_match(conn, head.id)
    _offsets_match(conn, tail.id)


def test_split_shifts_the_numbers_of_later_chapters(conn, marked):
    later = repo.create_chapter(conn, marked.book_id, 2, "Дальше", "Текст.\n\nЕщё текст.")
    library.split_chapter(conn, marked.id, marked.text.index("Он промолчал"))
    assert [c.number for c in repo.list_chapters(conn, marked.book_id)] == [1, 2, 3]
    assert repo.get_chapter(conn, later.id).number == 3


def test_split_at_the_first_paragraph_is_refused(conn, marked):
    with pytest.raises(library.LibraryError, match="первый абзац"):
        library.split_chapter(conn, marked.id, 3)


def test_split_refuses_a_segment_crossing_the_boundary(conn, chapter):
    second = chapter.text.index("— Ты всё-таки")
    repo.replace_segments(
        conn, chapter.id,
        [repo.Segment(speaker="narrator", text=chapter.text[: second + 10], char_start=0, char_end=second + 10)],
    )
    with pytest.raises(library.LibraryError, match="переходит"):
        library.split_chapter(conn, chapter.id, second)


def test_merge_restores_the_original_chapter(conn, marked):
    original = [(s.speaker, s.text) for s in repo.list_segments(conn, marked.id)]
    head, _tail = library.split_chapter(conn, marked.id, marked.text.index("Он промолчал"))

    merged = library.merge_with_next(conn, head.id)
    assert merged.text == marked.text
    assert [(s.speaker, s.text) for s in repo.list_segments(conn, merged.id)] == original
    assert len(repo.list_chapters(conn, marked.book_id)) == 1
    _offsets_match(conn, merged.id)


def test_merging_the_last_chapter_is_refused(conn, marked):
    with pytest.raises(library.LibraryError, match="последняя"):
        library.merge_with_next(conn, marked.id)


def test_split_clears_undo_history_of_the_chapter(conn, marked):
    first = repo.list_segments(conn, marked.id)[0]
    editing.assign_speaker(conn, "s1", marked.id, [first.id], "Аглая")
    library.split_chapter(conn, marked.id, marked.text.index("Он промолчал"))
    assert editing.history_state(conn, "s1", marked.id)["can_undo"] is False


# --------------------------------------------------------------------------
# Поиск
# --------------------------------------------------------------------------


def test_search_ignores_yo_and_marks_the_original_text(conn, marked):
    hits = [r for r in library.search_library(conn, "еще роняли") if r["kind"] == "chapter"]
    assert hits
    assert f"{repo.MARK_OPEN}ещё{repo.MARK_CLOSE}" in hits[0]["snippet"]


def test_title_match_ranks_above_text_match(conn, marked):
    repo.create_book(conn, "Колокол")  # в тексте главы тоже есть «колокол»
    results = library.search_library(conn, "колокол")
    assert results[0]["kind"] == "book"
    assert any(r["kind"] == "chapter" for r in results)


def test_query_without_words_finds_nothing(conn, marked):
    assert library.search_library(conn, "!!! ...") == []


def test_fts_operators_in_the_query_are_just_words():
    assert repo.fts_query("NOT рынок") == '"NOT"* "рынок"*'


# --------------------------------------------------------------------------
# Сборка глав из блоков
# --------------------------------------------------------------------------


def test_chapters_split_on_headings_and_markers():
    blocks = [
        (PARAGRAPH, "Аннотация."),
        (HEADING, "Часть первая"),
        (HEADING, "Глава 1. Начало"),
        (PARAGRAPH, "Текст первой."),
        (PARAGRAPH, "Глава 2"),
        (PARAGRAPH, "Текст второй."),
    ]
    chapters = chapters_from_blocks(blocks, min_chapter_chars=300)
    assert [(c.number, c.title) for c in chapters] == [(1, "Начало"), (2, "")]


def test_heading_without_number_becomes_a_titled_chapter():
    chapters = chapters_from_blocks([(HEADING, "Пролог"), (PARAGRAPH, "Было темно.")])
    assert [(c.number, c.title) for c in chapters] == [(1, "Пролог")]


def test_repeated_chapter_numbers_are_renumbered():
    chapters = [
        Chapter(number=n, title="", paragraphs=[Paragraph(0, "текст")]) for n in (1, 2, 1, 2)
    ]
    ensure_unique_numbers(chapters)
    assert [c.number for c in chapters] == [1, 2, 3, 4]
    assert [c.title for c in chapters] == ["", "", "Глава 1", "Глава 2"]


def test_pdf_lines_are_glued_back_into_paragraphs():
    pages = [[
        "Глава 1",
        "Он шёл по до-",
        "роге и молчал.",
        "— Стой! — крикнули сзади.",
        "12",
    ]]
    assert pdf_blocks(pages) == [
        (HEADING, "Глава 1"),
        (PARAGRAPH, "Он шёл по дороге и молчал."),
        (PARAGRAPH, "— Стой! — крикнули сзади."),
    ]


def test_pdf_running_headers_are_dropped():
    pages = [["Петрова. Туманный берег", f"Строка страницы {i}."] for i in range(6)]
    text = " ".join(t for _, t in pdf_blocks(pages))
    assert "Туманный берег" not in text
    assert "Строка страницы 5." in text


# --------------------------------------------------------------------------
# Форматы
# --------------------------------------------------------------------------

FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info>
  <author><first-name>Анна</first-name><last-name>Петрова</last-name></author>
  <book-title>Туманный берег</book-title>
  <coverpage><image l:href="#cover.png"/></coverpage>
</title-info></description>
<body>
  <title><p>Туманный берег</p></title>
  <section><title><p>Часть первая</p></title>
    <section><title><p>Глава 1</p><p>Прибытие</p></title>
      <p>Корабль вошёл в бухту на рассвете.</p>
      <p>— Земля! — крикнул юнга.</p>
    </section>
    <section><title><p>Глава 2</p></title>
      <epigraph><p>Всё проходит.</p></epigraph>
      <p>Берег встретил их тишиной.</p>
    </section>
  </section>
</body>
<body name="notes"><section><p>Сноска, которую озвучивать не надо.</p></section></body>
<binary id="cover.png" content-type="image/png">iVBORw0KGgo=</binary>
</FictionBook>
"""


def test_fb2_is_parsed_with_metadata_and_cover(tmp_path):
    source = tmp_path / "берег.fb2"
    source.write_text(FB2, encoding="utf-8")
    book = load_book(source)

    assert (book.title, book.author) == ("Туманный берег", "Анна Петрова")
    assert [(c.number, c.title) for c in book.chapters] == [(1, "Прибытие"), (2, "")]
    assert [p.text for p in book.chapters[1].paragraphs] == ["Всё проходит.", "Берег встретил их тишиной."]
    assert book.cover and book.cover.startswith(b"\x89PNG")
    assert "Сноска" not in " ".join(c.text for c in book.chapters)


def test_zipped_fb2_is_parsed(tmp_path):
    source = tmp_path / "берег.fb2.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("bereg.fb2", FB2.encode("utf-8"))
    assert len(load_book(source).chapters) == 2


def test_docx_chapters_follow_heading_styles(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.core_properties.title = "Документ"
    document.add_heading("Пролог", level=1)
    document.add_paragraph("Первый абзац пролога.")
    document.add_heading("Глава 1", level=2)
    document.add_paragraph("Первый абзац главы.")
    document.add_paragraph("Второй абзац главы.")
    source = tmp_path / "док.docx"
    document.save(source)

    book = load_book(source)
    assert book.title == "Документ"
    # Пролог без номера получает 1, и «Глава 1» с ним совпадает — её
    # перенумеровывают, сохраняя исходный номер в заголовке.
    assert [(c.number, c.title, len(c.paragraphs)) for c in book.chapters] == [
        (1, "Пролог", 1),
        (2, "Глава 1", 2),
    ]


def test_unsupported_format_names_the_supported_ones(tmp_path):
    source = tmp_path / "книга.rtf"
    source.write_text("{\\rtf1}", encoding="utf-8")
    with pytest.raises(ParserError, match="fb2"):
        load_book(source)


def test_import_stores_the_cover_inside_the_library(conn, library, tmp_path):
    source = tmp_path / "берег.fb2"
    source.write_text(FB2, encoding="utf-8")
    book = library_module_import(conn, source)
    assert book.cover_path and book.cover_path.startswith("books/")
    assert paths.absolute(book.cover_path).read_bytes().startswith(b"\x89PNG")


def test_delete_book_removes_its_files(conn, library, tmp_path):
    source = tmp_path / "берег.fb2"
    source.write_text(FB2, encoding="utf-8")
    book = library_module_import(conn, source)
    stored = paths.absolute(book.source_path)
    assert stored.is_file()

    library_module().delete_book(conn, book.id)
    assert not stored.exists()
    with pytest.raises(repo.RepoError):
        repo.get_book(conn, book.id)


def library_module():
    # Фикстура ``library`` затеняет модуль с тем же именем внутри тестов.
    from audiobook.core import library as module

    return module


def library_module_import(conn, source):
    return library_module().import_book(conn, source)
