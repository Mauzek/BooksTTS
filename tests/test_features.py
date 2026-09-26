"""Готовность книги к озвучке, «продолжить слушать», разметка книги целиком,
ключ разметки из системного хранилища и уборка за неудачным импортом."""

from __future__ import annotations

import pytest
from fakes import FakeClient, echo_narrator
from test_catalog import FakeKeyring

from audiobook import paths
from audiobook.core import checks, jobs, library, repo, secrets
from audiobook.core.markup import MarkupConfigError
from audiobook.core.models import JobStatus, Voice


@pytest.fixture()
def vault(monkeypatch):
    FakeKeyring.store = {}
    monkeypatch.setattr(secrets, "_keyring", FakeKeyring)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return FakeKeyring.store


def _voice(conn, key="aidar") -> Voice:
    return repo.upsert_voice(conn, Voice(engine="silero", voice_key=key, display_name=key))


# --------------------------------------------------------------------------
# Готовность книги
# --------------------------------------------------------------------------


def test_readiness_names_roles_without_voice(conn, marked):
    state = checks.book_readiness(conn, marked.book_id)
    assert state["ready"] is False
    assert {m["speaker"] for m in state["missing_voice"]} == {"narrator", "Велимир", "Аглая"}
    # Самые разговорчивые — первыми: их важнее озвучить.
    assert state["missing_voice"][0]["speaker"] == "narrator"


def test_unmarked_chapter_does_not_block_voicing(conn, marked):
    voice = _voice(conn)
    for speaker in repo.book_speakers(conn, marked.book_id):
        repo.set_cast(conn, marked.book_id, speaker, voice.id)
    repo.create_chapter(conn, marked.book_id, 2, "Без разметки", "Текст.")

    state = checks.book_readiness(conn, marked.book_id)
    assert state["ready"] is True
    assert [c["number"] for c in state["unmarked"]] == [2]


def test_voice_that_left_the_catalog_blocks(conn, marked):
    voice = _voice(conn)
    for speaker in repo.book_speakers(conn, marked.book_id):
        repo.set_cast(conn, marked.book_id, speaker, voice.id)
    repo.mark_missing_voices(conn, "silero", [])

    state = checks.book_readiness(conn, marked.book_id)
    assert state["ready"] is False
    assert {u["speaker"] for u in state["unavailable_voice"]} == {"narrator", "Велимир", "Аглая"}


def test_book_without_markup_is_not_ready(conn, chapter):
    state = checks.book_readiness(conn, chapter.book_id)
    assert state["ready"] is False and state["marked"] == 0


# --------------------------------------------------------------------------
# «Продолжить слушать»
# --------------------------------------------------------------------------


def test_progress_counts_listened_chapters_and_position(conn, marked):
    repo.update_chapter(conn, marked.id, audio_path="audio/1.mp3", duration_ms=1000)
    second = repo.create_chapter(conn, marked.book_id, 2, "Вторая", "Текст.")
    repo.update_chapter(conn, second.id, audio_path="audio/2.mp3", duration_ms=3000)
    repo.set_playback(conn, marked.book_id, marked.id, 1000)  # первая дослушана
    repo.set_playback(conn, marked.book_id, second.id, 1500)

    item = repo.recent_playback(conn)[0]
    assert item["chapter_label"] == "Глава 2. Вторая"
    assert item["listened_ms"] == 2500 and item["total_ms"] == 4000
    assert item["progress"] == pytest.approx(0.625)


def test_recent_books_come_first(conn, marked):
    other = repo.create_book(conn, "Другая")
    repo.set_playback(conn, marked.book_id, marked.id, 0)
    conn.execute("UPDATE playback SET updated_at = datetime('now', '-1 day')")
    repo.set_playback(conn, other.id, None, 0)
    assert [item["title"] for item in repo.recent_playback(conn)] == ["Другая", "Проба"]


# --------------------------------------------------------------------------
# Разметка книги целиком
# --------------------------------------------------------------------------


@pytest.fixture()
def unmarked_book(conn, library):
    book = repo.create_book(conn, "Две главы")
    for number in (1, 2):
        repo.create_chapter(
            conn, book.id, number, f"Глава {number}",
            f"Первый абзац главы {number}.\n\n— Реплика в главе {number}, — сказал он.",
        )
    conn.commit()
    return book


def test_book_markup_job_marks_every_chapter(db, conn, unmarked_book):
    queue = jobs.JobQueue(db, client=FakeClient(echo_narrator))
    job = queue.enqueue(jobs.KIND_MARKUP_BOOK, unmarked_book.id)
    assert "Разметка книги" in job.title

    result = queue.run_job(queue._claim())
    assert result["done"] == 2 and result["failed"] == []
    with db.connect() as check:
        for chapter in repo.list_chapters(check, unmarked_book.id):
            assert repo.list_segments(check, chapter.id)
        assert repo.get_job(check, job.id).status == JobStatus.DONE
        # Расход токенов записан по книге.
        assert repo.usage_summary(check)[0]["service"] == "anthropic"


def test_marked_chapters_are_left_alone_unless_forced(db, conn, unmarked_book):
    client = FakeClient(echo_narrator)
    queue = jobs.JobQueue(db, client=client)
    queue.enqueue(jobs.KIND_MARKUP_BOOK, unmarked_book.id)
    queue.run_job(queue._claim())
    calls = len(client.calls)

    queue.enqueue(jobs.KIND_MARKUP_BOOK, unmarked_book.id)
    result = queue.run_job(queue._claim())
    assert result["chapters"] == 0 and len(client.calls) == calls

    queue.enqueue(jobs.KIND_MARKUP_BOOK, unmarked_book.id, params={"force": True})
    assert queue.run_job(queue._claim())["chapters"] == 2


def test_missing_key_stops_the_whole_book(db, conn, unmarked_book, monkeypatch):
    """Без ключа упадёт каждая глава — не надо собирать пятьдесят одинаковых отказов."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise MarkupConfigError("не найден ключ Anthropic")

    monkeypatch.setattr(library, "markup_chapter", refuse)
    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_MARKUP_BOOK, unmarked_book.id)
    with pytest.raises(MarkupConfigError):
        queue.run_job(queue._claim())

    assert len(calls) == 1
    with db.connect() as check:
        failed = repo.get_job(check, job.id)
        assert failed.status == JobStatus.FAILED and "ключ" in failed.error


# --------------------------------------------------------------------------
# Ключ и адрес для разметки
# --------------------------------------------------------------------------


def test_markup_takes_the_key_from_the_system_vault(conn, marked, vault, monkeypatch):
    """Ключ, сохранённый на экране настроек, должен доходить до клиента."""
    secrets.set_key("anthropic", "sk-из-хранилища")
    repo.set_setting(conn, library.MARKUP_URL_SETTING, "https://proxy.example:8443")
    repo.set_setting(conn, library.MARKUP_MODEL_SETTING, "claude-sonnet-5")
    seen = {}

    def fake_markup_text(text, **kwargs):
        seen.update(kwargs)
        raise MarkupConfigError("дальше не нужно")

    monkeypatch.setattr(library, "markup_text", fake_markup_text)
    with pytest.raises(MarkupConfigError):
        library.markup_chapter(conn, marked.id)

    assert seen["api_key"] == "sk-из-хранилища"
    assert seen["base_url"] == "https://proxy.example:8443"
    assert seen["model"] == "claude-sonnet-5"


def test_explicit_arguments_win_over_settings(conn, vault):
    secrets.set_key("anthropic", "sk-из-хранилища")
    repo.set_setting(conn, library.MARKUP_MODEL_SETTING, "из-настроек")
    options = library.markup_connection(conn, api_key="явный", model="явная")
    assert options["api_key"] == "явный" and options["model"] == "явная"


def test_no_key_anywhere_leaves_it_to_the_sdk(conn, vault):
    assert library.markup_connection(conn)["api_key"] is None


# --------------------------------------------------------------------------
# Импорт убирает за собой
# --------------------------------------------------------------------------


def test_failed_import_leaves_no_orphan_copy(conn, library, tmp_path, monkeypatch):
    source = tmp_path / "книга.txt"
    source.write_text("Глава 1\n\n" + "\n\n".join(f"Абзац {i}." for i in range(5)), encoding="utf-8")

    def broken(*args, **kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(repo, "create_chapter", broken)
    with pytest.raises(RuntimeError):
        library_module().import_book(conn, source)
    assert list(paths.books_dir().iterdir()) == []


def library_module():
    # Фикстура ``library`` затеняет модуль внутри тестов, где она запрошена.
    from audiobook.core import library as module

    return module
