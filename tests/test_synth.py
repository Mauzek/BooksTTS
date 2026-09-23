"""Озвучка глав, словарь произношений, склейка и очередь задач."""

from __future__ import annotations

import pytest

from audiobook import paths
from audiobook.core import catalog, jobs, pronounce, repo, synth
from audiobook.core.engines import EngineError, EngineFatalError
from audiobook.core.engines import silero as silero_module
from audiobook.core.models import JobStatus, Voice

# Секунда тишины на 48 кГц: настоящий wav, который переварит pydub.
SILENT_WAV = silero_module.wav_bytes([0] * 4800, 48000)


class RecordingEngine:
    """Считает вызовы и отдаёт настоящий (пустой) wav."""

    name = "silero"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_for: set[str] = set()
        self.fatal_for: set[str] = set()
        self.fail_times: dict[str, int] = {}

    def synthesize(self, text, voice_key, *, rate=1.0, pitch=1.0, volume=1.0):
        self.calls.append((text, voice_key))
        if text in self.fatal_for:
            raise EngineFatalError("нет ключа")
        if text in self.fail_for:
            raise EngineError("движок не отвечает")
        left = self.fail_times.get(text, 0)
        if left:
            self.fail_times[text] = left - 1
            raise EngineError("временный сбой")
        return SILENT_WAV, "wav"


@pytest.fixture()
def engine(monkeypatch):
    fake = RecordingEngine()
    monkeypatch.setattr(catalog, "engine_for", lambda conn, name: fake)
    monkeypatch.setattr(synth, "BACKOFF", (0.0, 0.0))  # тесты не ждут по-настоящему
    return fake


@pytest.fixture()
def voiced(conn, marked, library):
    """Размеченная глава, у всех ролей есть голос."""
    voice = repo.upsert_voice(conn, Voice(engine="silero", voice_key="aidar", display_name="aidar"))
    for speaker in repo.book_speakers(conn, marked.book_id):
        repo.set_cast(conn, marked.book_id, speaker, voice.id)
    return marked


# --------------------------------------------------------------------------
# Словарь произношений
# --------------------------------------------------------------------------


def test_dictionary_replaces_whole_words_only():
    rules = [{"term": "Рудеус", "replacement": "Рудэус", "whole_word": True}]
    assert pronounce.apply("Рудеус сказал", rules) == "Рудэус сказал"
    assert pronounce.apply("Рудеусом сказал", rules) == "Рудеусом сказал"


def test_dictionary_ignores_case_unless_asked():
    assert pronounce.apply("рудеус", [{"term": "Рудеус", "replacement": "Рудэус"}]) == "Рудэус"
    strict = [{"term": "Рудеус", "replacement": "Рудэус", "case_sensitive": True}]
    assert pronounce.apply("рудеус", strict) == "рудеус"


def test_longer_terms_win():
    rules = [
        {"term": "Рудеус", "replacement": "Рудэус"},
        {"term": "Рудеус Грейрат", "replacement": "Рудэус Грэйрат"},
    ]
    assert pronounce.apply("Рудеус Грейрат вошёл", rules) == "Рудэус Грэйрат вошёл"


def test_replacement_with_backslashes_is_literal():
    rules = [{"term": "имя", "replacement": r"\1 и \n"}]
    assert pronounce.apply("имя", rules) == r"\1 и \n"


def test_affected_lists_terms_found_in_text():
    rules = [{"term": "Аглая", "replacement": "Аглаjа"}, {"term": "Терех", "replacement": "Тэрех"}]
    assert pronounce.affected("Аглая молчала", rules) == ["Аглая"]


# --------------------------------------------------------------------------
# План озвучки
# --------------------------------------------------------------------------


def test_plan_names_speakers_without_voices(conn, marked):
    prepared = synth.plan(conn, marked.id)
    assert set(prepared["missing_voice"]) == {"narrator", "Велимир", "Аглая"}


def test_synthesis_refuses_until_voices_are_assigned(conn, marked, engine):
    with pytest.raises(synth.SynthError, match="нет голоса"):
        synth.synthesize_chapter(conn, marked.id)


# --------------------------------------------------------------------------
# Озвучка
# --------------------------------------------------------------------------


def test_chapter_is_voiced_once_and_reused(conn, voiced, engine):
    first = synth.synthesize_chapter(conn, voiced.id)
    assert first["done"] == 5 and not first["failed"]
    segments = repo.list_segments(conn, voiced.id)
    assert all(s.audio_path and paths.absolute(s.audio_path).is_file() for s in segments)

    engine.calls.clear()
    second = synth.synthesize_chapter(conn, voiced.id)
    assert second["total"] == 0 and engine.calls == []


def test_editing_one_line_revoices_only_it(conn, voiced, engine):
    synth.synthesize_chapter(conn, voiced.id)
    target = repo.list_segments(conn, voiced.id)[1]
    repo.update_segment(conn, target.id, text="Ты всё же пришла")

    engine.calls.clear()
    result = synth.synthesize_chapter(conn, voiced.id)
    assert result["total"] == 1
    assert [text for text, _voice in engine.calls] == ["Ты всё же пришла"]


def test_pronunciation_change_revoices_only_affected_lines(conn, voiced, engine):
    synth.synthesize_chapter(conn, voiced.id)
    # «колокол» встречается ровно в одной реплике главы.
    repo.set_pronunciation(conn, voiced.book_id, "колокол", "ко́локол")

    engine.calls.clear()
    result = synth.synthesize_chapter(conn, voiced.id)
    assert result["total"] == 1
    assert "ко́локол" in engine.calls[0][0]  # в движок ушёл заменённый текст
    assert "колокол " not in engine.calls[0][0]

    # Слово, которого в главе нет, не трогает ничего.
    engine.calls.clear()
    repo.set_pronunciation(conn, voiced.book_id, "дракон", "драко́н")
    assert synth.synthesize_chapter(conn, voiced.id)["total"] == 0
    assert engine.calls == []


def test_emotion_counts_only_for_engines_that_hear_it(conn, voiced, engine):
    """Смена эмоции переозвучивает реплику у Qwen, но не у Silero — ему всё равно."""
    synth.synthesize_chapter(conn, voiced.id)
    target = repo.list_segments(conn, voiced.id)[1]
    repo.update_segment(conn, target.id, emotion="зло")
    assert synth.plan(conn, voiced.id)["pending"] == []  # у всех ролей Silero

    qwen = repo.upsert_voice(conn, Voice(engine="qwen", voice_key="Ryan", display_name="Ryan"))
    repo.set_cast(conn, voiced.book_id, target.speaker, qwen.id)
    before = {p[0].id: p[3] for p in synth.plan(conn, voiced.id)["pending"]}
    repo.update_segment(conn, target.id, emotion="радостно")
    after = {p[0].id: p[3] for p in synth.plan(conn, voiced.id)["pending"]}
    assert before[target.id] != after[target.id]


def test_changing_the_voice_revoices_the_whole_role(conn, voiced, engine):
    synth.synthesize_chapter(conn, voiced.id)
    other = repo.upsert_voice(conn, Voice(engine="silero", voice_key="baya", display_name="baya"))
    repo.set_cast(conn, voiced.book_id, "Аглая", other.id)

    engine.calls.clear()
    result = synth.synthesize_chapter(conn, voiced.id)
    assert result["total"] == 1
    assert engine.calls[0][1] == "baya"


def test_a_failed_line_does_not_stop_the_chapter(conn, voiced, engine):
    engine.fail_for = {"сказал он."}
    result = synth.synthesize_chapter(conn, voiced.id)
    assert len(result["failed"]) == 1
    assert result["done"] == 5
    broken = next(s for s in repo.list_segments(conn, voiced.id) if s.text == "сказал он.")
    assert "не отвечает" in broken.error
    assert sum(1 for s in repo.list_segments(conn, voiced.id) if s.audio_path) == 4


def test_temporary_failure_is_retried(conn, voiced, engine):
    engine.fail_times = {"сказал он.": 2}  # две неудачи, третья попытка удачная
    result = synth.synthesize_chapter(conn, voiced.id)
    assert result["failed"] == []
    assert sum(1 for text, _ in engine.calls if text == "сказал он.") == 3


def test_hopeless_failure_is_not_retried(conn, voiced, engine):
    """Ключ не появится от повтора — три попытки с паузами были бы тратой времени."""
    engine.fatal_for = {"сказал он."}
    result = synth.synthesize_chapter(conn, voiced.id)
    assert len(result["failed"]) == 1
    assert sum(1 for text, _ in engine.calls if text == "сказал он.") == 1


def test_synthesis_stops_when_asked(conn, voiced, engine):
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2

    result = synth.synthesize_chapter(conn, voiced.id, should_stop=should_stop)
    assert result["cancelled"] is True
    assert result["done"] < 5


def test_parallel_synthesis_voices_everything(conn, voiced, engine):
    result = synth.synthesize_chapter(conn, voiced.id, parallelism=3)
    assert result["done"] == 5 and not result["failed"]
    assert all(s.audio_path for s in repo.list_segments(conn, voiced.id))


# --------------------------------------------------------------------------
# Склейка
# --------------------------------------------------------------------------


def test_assembly_stores_the_timeline(conn, voiced, engine):
    synth.synthesize_chapter(conn, voiced.id)
    result = synth.assemble_chapter(conn, voiced.id)

    assert result["duration_ms"] > 0
    assert paths.absolute(result["path"]).is_file()
    segments = repo.list_segments(conn, voiced.id)
    assert all(s.audio_start_ms is not None for s in segments)
    # Реплики идут подряд и не налезают друг на друга.
    starts = [s.audio_start_ms for s in segments]
    assert starts == sorted(starts)
    assert all(a.audio_end_ms <= b.audio_start_ms for a, b in zip(segments, segments[1:]))
    chapter = repo.get_chapter(conn, voiced.id)
    assert chapter.audio_path == result["path"] and chapter.duration_ms == result["duration_ms"]


def test_assembly_is_reused_until_something_changes(conn, voiced, engine):
    synth.synthesize_chapter(conn, voiced.id)
    synth.assemble_chapter(conn, voiced.id)
    assert synth.assemble_chapter(conn, voiced.id)["reused"] is True

    target = repo.list_segments(conn, voiced.id)[0]
    repo.update_segment(conn, target.id, text="Другой текст")
    synth.synthesize_chapter(conn, voiced.id)
    assert synth.assemble_chapter(conn, voiced.id)["reused"] is False


def test_assembly_needs_at_least_one_voiced_line(conn, voiced, engine):
    from audiobook.core.assemble import AssembleError

    with pytest.raises(AssembleError, match="ни одна реплика"):
        synth.assemble_chapter(conn, voiced.id)


# --------------------------------------------------------------------------
# Очередь задач
# --------------------------------------------------------------------------


def test_queue_runs_a_book_job(db, conn, voiced, engine):
    conn.commit()
    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_BOOK, voiced.book_id)
    assert "Озвучка книги" in job.title

    claimed = queue._claim()
    assert claimed.status == JobStatus.RUNNING
    result = queue.run_job(claimed)

    assert result["chapters"] == 1 and not result["failed"]
    with db.connect() as check:
        finished = repo.get_job(check, job.id)
        assert finished.status == JobStatus.DONE
        assert finished.progress == 1.0 and finished.finished_at
        assert repo.get_chapter(check, voiced.id).audio_path


def test_unmarked_chapters_are_skipped_not_failed(db, conn, voiced, engine):
    """В книге может быть глава без разметки — это не ошибка озвучки."""
    repo.create_chapter(conn, voiced.book_id, 2, "Без разметки", "Просто текст.")
    conn.commit()

    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_BOOK, voiced.book_id)
    result = queue.run_job(queue._claim())

    assert result["skipped"] == 1 and result["failed"] == []
    with db.connect() as check:
        assert repo.get_job(check, job.id).status == JobStatus.DONE


def test_pending_job_is_cancelled_outright(db, conn, voiced):
    conn.commit()
    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_SYNTHESIS, voiced.id)
    assert queue.cancel(job.id).status == JobStatus.CANCELLED
    assert queue._claim() is None


def test_running_job_is_asked_to_stop(db, conn, voiced, engine):
    conn.commit()
    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_SYNTHESIS, voiced.id)
    claimed = queue._claim()
    queue.cancel(claimed.id)  # пока «выполняется» — просим остановиться

    result = queue.run_job(claimed)
    assert result["cancelled"] is True
    with db.connect() as check:
        assert repo.get_job(check, job.id).status == JobStatus.CANCELLED


def test_interrupted_jobs_return_to_the_queue(db, conn, voiced):
    conn.commit()
    queue = jobs.JobQueue(db)
    running = queue.enqueue(jobs.KIND_BOOK, voiced.book_id)
    queue._claim()  # как будто приложение закрыли прямо во время работы

    assert queue.recover() == {"resumed": 1, "cancelled": 0}
    with db.connect() as check:
        assert repo.get_job(check, running.id).status == JobStatus.PENDING


def test_failed_job_keeps_the_reason(db, conn, marked):
    conn.commit()
    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_SYNTHESIS, marked.id)  # голоса не назначены
    claimed = queue._claim()
    with pytest.raises(synth.SynthError):
        queue.run_job(claimed)
    with db.connect() as check:
        failed = repo.get_job(check, job.id)
        assert failed.status == JobStatus.FAILED and "нет голоса" in failed.error


def test_finished_jobs_wait_to_be_announced(db, conn, voiced, engine):
    conn.commit()
    queue = jobs.JobQueue(db)
    job = queue.enqueue(jobs.KIND_BOOK, voiced.book_id)
    queue.run_job(queue._claim())

    with db.connect() as check:
        assert [j.id for j in repo.unnotified_jobs(check)] == [job.id]
        repo.update_job_fields(check, job.id, notified=True)
        assert repo.unnotified_jobs(check) == []
