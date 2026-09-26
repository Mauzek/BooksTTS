"""Сведения о книге, своя обложка и главы, добавленные в готовую книгу."""

from __future__ import annotations

import json

import pytest
from fakes import FakeClient

from audiobook.api.app import create_app
from audiobook import paths
from audiobook.core import bookinfo, library, repo

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CSRF = {"x-requested-with": "booktts"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def suggestion(**overrides) -> str:
    data = {
        "recognized": True,
        "title": "Реинкарнация безработного",
        "author": "Рифудзин на Магонотэ",
        "year": 2012,
        "country": "Япония",
        "genres": ["фэнтези", "Исекай", "Космоопера"],
        "moods": ["Эпичное", "светлое", "Бодрое"],
        "tags": ["Перерождение", "магия", "исекай", "магия"],
        "description": "Безработный затворник погибает и рождается заново в мире магии.",
        "note": "",
    }
    data.update(overrides)
    return "Вот карточка:\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


# --------------------------------------------------------------------------
# Хранение
# --------------------------------------------------------------------------


def test_new_book_has_empty_info(conn):
    book = repo.create_book(conn, title="Проба")
    assert (book.year, book.country, book.description) == (None, "", "")
    assert (book.genres, book.moods, book.tags) == ([], [], [])


def test_labels_are_cleaned_and_year_can_be_cleared(conn, chapter):
    book = repo.update_book(
        conn, chapter.book_id,
        genres=[" Фэнтези ", "фэнтези", "", "Исекай"], tags=["магия"] * 3, year="2012",
    )
    assert book.genres == ["Фэнтези", "Исекай"]
    assert book.tags == ["магия"]
    assert book.year == 2012
    assert repo.update_book(conn, chapter.book_id, year=None).year is None
    with pytest.raises(repo.InvalidOperation):
        repo.update_book(conn, chapter.book_id, year="двенадцатый")


# --------------------------------------------------------------------------
# Подбор через Claude
# --------------------------------------------------------------------------


def test_suggestion_keeps_only_known_genres_and_moods():
    parsed = bookinfo.parse_suggestion(suggestion())
    # Исекай — не книжный жанр: он остаётся меткой.
    assert parsed["genres"] == ["Фэнтези"]
    assert parsed["moods"] == ["Эпичное", "Светлое"]
    assert parsed["tags"] == ["перерождение", "магия", "исекай"]
    assert parsed["year"] == 2012


def test_garbage_answer_is_a_clear_error():
    with pytest.raises(bookinfo.BookInfoError):
        bookinfo.parse_suggestion("не знаю такой книги")


def test_known_book_is_described_by_title_alone(conn, chapter):
    """Узнанную книгу описываем целиком — отрывок текста не нужен и не шлётся."""
    client = FakeClient(lambda message, _n: suggestion())
    result = bookinfo.suggest_info(
        conn, chapter.book_id, title="Реинкарнация безработного", country="Япония", client=client,
    )
    assert result["author"] == "Рифудзин на Магонотэ"
    assert len(client.calls) == 1
    sent = client.calls[0]["messages"][0]["content"]
    assert "Название: Реинкарнация безработного" in sent
    assert "Страна: Япония" in sent
    assert "Автор: не указан" in sent
    assert chapter.text[:40] not in sent
    usage = conn.execute("SELECT operation, book_id FROM api_usage").fetchall()
    assert [tuple(row) for row in usage] == [("book-info", chapter.book_id)]


def test_unknown_book_is_described_by_its_opening(conn, chapter):
    """Не узнал по названию — второй запрос несёт начало текста."""
    replies = [suggestion(recognized=False, genres=[], moods=[], tags=[], description=""),
               suggestion(recognized=False, author="")]
    client = FakeClient(lambda message, n: replies[n - 1])
    result = bookinfo.suggest_info(conn, chapter.book_id, title="Ночной рынок", client=client)
    assert len(client.calls) == 2
    assert chapter.text[:40] not in client.calls[0]["messages"][0]["content"]
    assert chapter.text[:40] in client.calls[1]["messages"][0]["content"]
    assert result["from_excerpt"] is True and result["genres"] == ["Фэнтези"]


def test_suggest_info_needs_a_title(conn, chapter):
    with pytest.raises(bookinfo.BookInfoError, match="название"):
        bookinfo.suggest_info(conn, chapter.book_id, title="  ", client=FakeClient(lambda *_: ""))


# --------------------------------------------------------------------------
# Главы в готовой книге
# --------------------------------------------------------------------------


def test_pasted_chapter_goes_to_the_end_and_takes_its_heading(conn, chapter):
    created = library.add_chapter_from_text(
        conn, chapter.book_id, "Глава 2. Дорога\n\nОни вышли на рассвете.\n\nШли молча."
    )
    assert (created.number, created.title) == (2, "Дорога")
    assert created.text == "Они вышли на рассвете.\n\nШли молча."


def test_chapter_can_be_inserted_between_others(conn, chapter):
    library.add_chapter_from_text(conn, chapter.book_id, "Последняя глава.", title="Эпилог")
    middle = library.add_chapter_from_text(conn, chapter.book_id, "Середина.", title="Вставка", after=1)
    titles = [(c.number, c.title) for c in repo.list_chapters(conn, chapter.book_id)]
    assert titles == [(1, chapter.title), (2, "Вставка"), (3, "Эпилог")]
    assert middle.number == 2


def test_empty_pasted_chapter_is_rejected(conn, chapter):
    with pytest.raises(library.LibraryError):
        library.add_chapter_from_text(conn, chapter.book_id, " \n\n ")


def test_chapters_from_a_file(conn, chapter, tmp_path):
    source = tmp_path / "продолжение.txt"
    source.write_text(
        "Глава 1. Утро\n\n" + "Первый абзац утра. " * 30 + "\n\nГлава 2. Вечер\n\n" + "Вечерний абзац. " * 30,
        encoding="utf-8",
    )
    created = library.add_chapters_from_file(conn, chapter.book_id, source)
    assert [(c.number, c.title) for c in created] == [(2, "Утро"), (3, "Вечер")]


def test_file_without_headings_becomes_one_chapter_named_after_it(conn, chapter, tmp_path):
    source = tmp_path / "Бонусная глава.txt"
    source.write_text("Просто текст без заголовков. " * 20, encoding="utf-8")
    (created,) = library.add_chapters_from_file(conn, chapter.book_id, source)
    assert created.title == "Бонусная глава"


def test_removing_a_chapter_closes_the_gap(conn, chapter):
    second = library.add_chapter_from_text(conn, chapter.book_id, "Вторая.", title="Два")
    third = library.add_chapter_from_text(conn, chapter.book_id, "Третья.", title="Три")
    library.remove_chapter(conn, second.id)
    assert [(c.id, c.number) for c in repo.list_chapters(conn, chapter.book_id)] == [(chapter.id, 1), (third.id, 2)]
    library.remove_chapter(conn, third.id)
    with pytest.raises(library.LibraryError, match="единственная"):
        library.remove_chapter(conn, chapter.id)


# --------------------------------------------------------------------------
# Обложка
# --------------------------------------------------------------------------


def test_own_cover_replaces_the_old_one_and_can_be_removed(conn, chapter):
    root = paths.library_root()
    first = library.set_cover(conn, chapter.book_id, PNG).cover_path
    second = library.set_cover(conn, chapter.book_id, PNG).cover_path
    assert not (root / first).exists()  # прежняя картинка не копится
    assert (root / second).is_file()
    assert repo.get_book(conn, chapter.book_id).cover_path == second
    assert library.remove_cover(conn, chapter.book_id).cover_path is None
    assert not (root / second).exists()


def test_cover_must_be_an_image(conn, chapter):
    with pytest.raises(library.LibraryError, match="картинка"):
        library.set_cover(conn, chapter.book_id, b"%PDF-1.7 not an image")


# --------------------------------------------------------------------------
# Прослушанные главы
# --------------------------------------------------------------------------


def voiced(conn, book_id, number, duration_ms=60_000):
    chapter = repo.get_chapter_by_number(conn, book_id, number)
    return repo.update_chapter(conn, chapter.id, audio_path=f"audio/{number}.mp3", duration_ms=duration_ms)


def test_chapter_heard_to_the_end_stays_listened_when_replayed(conn, chapter):
    book = chapter.book_id
    library.add_chapter_from_text(conn, book, "Вторая глава.", title="Два")
    first, second = voiced(conn, book, 1), voiced(conn, book, 2)
    repo.set_playback(conn, book, first.id, 30_000)
    assert repo.get_chapter(conn, first.id).listened_at is None  # середина — ещё не дослушана
    repo.set_playback(conn, book, first.id, 58_000)  # хвост тишины не обязателен
    repo.set_playback(conn, book, second.id, 60_000)
    repo.set_playback(conn, book, first.id, 0)  # начали книгу заново
    assert repo.get_chapter(conn, first.id).listened_at
    assert repo.get_chapter(conn, second.id).listened_at
    (entry,) = repo.recent_playback(conn)
    assert entry["progress"] == 0.5  # вторая дослушана, первая слушается с начала


def test_listened_mark_can_be_toggled_by_hand(conn, chapter):
    assert repo.set_chapter_listened(conn, chapter.id, True).listened_at
    assert repo.set_chapter_listened(conn, chapter.id, False).listened_at is None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


@pytest.fixture()
def http(db, conn, chapter):
    conn.commit()
    app = create_app(db=db, client=FakeClient(lambda message, _n: suggestion()))
    with fastapi_testclient.TestClient(app, headers=CSRF) as client:
        yield client


def test_book_info_round_trip_over_http(http, chapter):
    url = f"/api/books/{chapter.book_id}"
    saved = http.patch(url, json={"year": 1999, "genres": ["Детектив"], "moods": ["Мрачное"]}).json()
    assert (saved["year"], saved["genres"], saved["moods"]) == (1999, ["Детектив"], ["Мрачное"])
    cleared = http.patch(url, json={"year": None}).json()
    assert cleared["year"] is None and cleared["genres"] == ["Детектив"]
    assert cleared["info_source"] == ""
    from_ai = http.patch(url, json={"description": "Кто-то кого-то убил.", "info_source": "ai"}).json()
    assert from_ai["info_source"] == "ai" and from_ai["info_at"]
    assert http.get(url).json()["book"]["moods"] == ["Мрачное"]


def test_describe_and_vocabulary(http, chapter):
    vocabulary = http.get("/api/book-vocabulary").json()
    assert "Фэнтези" in vocabulary["genres"] and "Мрачное" in vocabulary["moods"]
    result = http.post(f"/api/books/{chapter.book_id}/describe", json={"title": "Реинкарнация безработного"}).json()
    assert result["genres"] == ["Фэнтези"]


def test_chapter_endpoints(http, chapter, tmp_path):
    book = chapter.book_id
    added = http.post(f"/api/books/{book}/chapters", json={"text": "Новый текст.", "title": "Новая"}).json()
    (new,) = added["chapters"]
    assert new["number"] == 2
    source = tmp_path / "ещё.txt"
    source.write_text("Ещё одна глава. " * 30, encoding="utf-8")
    from_file = http.post(f"/api/books/{book}/chapters/import", json={"path": str(source), "after": 1}).json()
    assert [c["number"] for c in from_file["chapters"]] == [2]
    uploaded = http.put(
        f"/api/books/{book}/chapters/upload?filename=bonus.txt", content="Бонус. ".encode() * 60,
    ).json()
    assert uploaded["chapters"][0]["title"] == "bonus"
    assert http.delete(f"/api/chapters/{new['id']}").status_code == 200
    numbers = [c["number"] for c in http.get(f"/api/books/{book}").json()["chapters"]]
    assert numbers == [1, 2, 3]


def test_cover_endpoints(http, chapter):
    book = chapter.book_id
    assert http.put(f"/api/books/{book}/cover", content=PNG).json()["cover_path"]
    assert http.get(f"/api/books/{book}/cover").status_code == 200
    assert http.delete(f"/api/books/{book}/cover").json()["cover_path"] is None
    listened = http.put(f"/api/chapters/{chapter.id}/listened", json={"listened": True}).json()
    assert listened["listened_at"]
    bad = http.put(f"/api/books/{book}/cover", content=b"hello")
    assert bad.status_code == 400 and "картинка" in bad.json()["detail"]


def test_validation_errors_are_in_russian(http, chapter):
    response = http.patch(f"/api/books/{chapter.book_id}", json={"year": "в прошлом веке"})
    assert response.status_code == 422
    assert response.json()["detail"] == "проверьте данные: год — нужно целое число"
