"""Очередь фоновых задач, переживающая закрытие приложения.

Всё состояние — в таблице ``job``: при старте незавершённое возвращается в
очередь и продолжается с того места, где остановилось. Продолжение дешёвое,
потому что озвучка идемпотентна — готовые реплики пропускаются.

Задачи выполняются по одной. Параллельность настраивается внутри озвучки
главы: две главы одновременно дрались бы за одну модель и за диск.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Callable

from . import export as export_mod
from . import library, repo, synth
from .catalog import settings
from .db import Database
from .markup import MarkupConfigError
from .models import Job, JobStatus

__all__ = ["JobQueue", "KIND_MARKUP", "KIND_SYNTHESIS", "KIND_BOOK", "KIND_FOLDER", "KINDS"]

log = logging.getLogger("audiobook.jobs")

KIND_MARKUP = "markup"
KIND_SYNTHESIS = "synthesis"  # одна глава
KIND_BOOK = "book"  # вся книга
KIND_FOLDER = "folder"  # все книги папки
KIND_EXPORT = "export"  # m4b, mp3 или разметка
KIND_MARKUP_BOOK = "markup_book"  # разметка всех глав книги
KINDS = (KIND_MARKUP, KIND_SYNTHESIS, KIND_BOOK, KIND_FOLDER, KIND_EXPORT, KIND_MARKUP_BOOK)

EXPORT_TITLES = {"m4b": "Экспорт m4b", "mp3": "Экспорт mp3", "json": "Экспорт разметки"}

CANCELLING = "cancelling"
POLL_SECONDS = 0.5


class JobQueue:
    """Одна рабочая нить, читающая задачи из базы."""

    def __init__(
        self,
        db: Database,
        *,
        client: Any = None,
        base_url: str | None = None,
        poll: float = POLL_SECONDS,
    ) -> None:
        self.db = db
        self.client = client
        self.base_url = base_url
        self.poll = poll
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ---------------- жизненный цикл ----------------

    def start(self) -> "JobQueue":
        self.recover()
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jobs", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def recover(self) -> dict[str, int]:
        """После перезапуска: прерванное — обратно в очередь, отменённое — закрыть."""
        with self.db.connect() as conn:
            resumed = conn.execute(
                "UPDATE job SET status = ?, updated_at = datetime('now') WHERE status = ?",
                (JobStatus.PENDING, JobStatus.RUNNING),
            ).rowcount
            closed = conn.execute(
                "UPDATE job SET status = ?, finished_at = datetime('now') WHERE status = ?",
                (JobStatus.CANCELLED, CANCELLING),
            ).rowcount
        if resumed or closed:
            log.info("восстановлено задач: %s, отменено: %s", resumed, closed)
        return {"resumed": resumed, "cancelled": closed}

    # ---------------- очередь ----------------

    def enqueue(
        self, kind: str, target_id: int | None = None, title: str = "",
        params: dict[str, Any] | None = None,
    ) -> Job:
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид задачи {kind!r}; есть: {', '.join(KINDS)}")
        import json

        with self.db.connect() as conn:
            job = repo.create_job(conn, kind, target_id)
            repo.update_job_fields(
                conn, job.id, title=title or _default_title(conn, kind, target_id),
                payload=json.dumps(params or {}, ensure_ascii=False),
            )
            job = repo.get_job(conn, job.id)
        self._wake.set()
        return job

    def cancel(self, job_id: int) -> Job:
        """Ожидающую задачу снимаем сразу, выполняемую — просим остановиться."""
        with self.db.connect() as conn:
            job = repo.get_job(conn, job_id)
            if job.status == JobStatus.PENDING:
                return repo.update_job(conn, job_id, status=JobStatus.CANCELLED)
            if job.status == JobStatus.RUNNING:
                return repo.update_job(conn, job_id, status=CANCELLING)
            return job

    def _claim(self) -> Job | None:
        """Взять следующую задачу. Одним запросом: очередь может читаться не только нами."""
        with self.db.connect() as conn:
            row = conn.execute(
                "UPDATE job SET status = ?, started_at = datetime('now'), "
                "updated_at = datetime('now') WHERE id = ("
                "  SELECT id FROM job WHERE status = ? ORDER BY id LIMIT 1"
                ") RETURNING *",
                (JobStatus.RUNNING, JobStatus.PENDING),
            ).fetchone()
            return Job.from_row(row) if row else None

    def _cancelling(self, job_id: int) -> bool:
        with self.db.connect() as conn:
            return _is_cancelling(conn, job_id)

    def _loop(self) -> None:  # pragma: no cover — рабочая нить
        while not self._stop.is_set():
            job = self._claim()
            if job is None:
                self._wake.wait(self.poll)
                self._wake.clear()
                continue
            try:
                self.run_job(job)
            except Exception as exc:  # noqa: BLE001 — задача не должна ронять нить
                log.exception("задача %s упала", job.id)
                with self.db.connect() as conn:
                    repo.update_job(conn, job.id, status=JobStatus.FAILED, error=str(exc)[:500])
                    repo.finish_job(conn, job.id)

    # ---------------- выполнение ----------------

    def run_job(self, job: Job) -> dict[str, Any]:
        """Выполнить задачу целиком. Вызывается нитью очереди и тестами напрямую."""
        handlers = {
            KIND_MARKUP: self._run_markup,
            KIND_SYNTHESIS: self._run_chapter,
            KIND_BOOK: self._run_book,
            KIND_FOLDER: self._run_folder,
            KIND_EXPORT: self._run_export,
            KIND_MARKUP_BOOK: self._run_markup_book,
        }
        try:
            result = handlers[job.kind](job)
        except Exception as exc:  # noqa: BLE001 — записываем причину и идём дальше
            with self.db.connect() as conn:
                repo.update_job(conn, job.id, status=JobStatus.FAILED, error=str(exc)[:500])
                repo.finish_job(conn, job.id)
            raise

        status = JobStatus.CANCELLED if result.get("cancelled") else JobStatus.DONE
        with self.db.connect() as conn:
            repo.update_job(conn, job.id, status=status, error=result.get("error"))
            repo.finish_job(conn, job.id)
        return result

    def _progress(self, conn: sqlite3.Connection, job_id: int):
        """Прогресс пишется тем же соединением, что и сама работа.

        Второе соединение здесь — взаимная блокировка: внешняя запись ждёт
        внутреннюю, а та — освобождения базы.
        """
        def report(done: int, total: int) -> None:
            repo.update_job(conn, job_id, done=done, total=total)
            conn.commit()  # иначе прогресс не виден интерфейсу до конца задачи
        return report

    def _markup_one(self, conn, chapter_id: int, params: dict[str, Any], report) -> Any:
        """Разметить главу и записать расход токенов."""
        result = library.markup_chapter(
            conn, chapter_id, client=self.client, base_url=self.base_url,
            # Пусто — модель из настроек библиотеки.
            model=params.get("model") or None,
            batch_chars=int(params.get("batch_chars") or library.DEFAULT_BATCH_CHARS),
            force=bool(params.get("force")),
            on_progress=report,
        )
        if result.usage:
            chapter = repo.get_chapter(conn, chapter_id)
            repo.record_usage(
                conn, "anthropic", "markup", chars=len(chapter.text),
                input_tokens=result.usage.get("input_tokens", 0),
                output_tokens=result.usage.get("output_tokens", 0),
                book_id=chapter.book_id,
            )
        return result

    def _run_markup(self, job: Job) -> dict[str, Any]:
        with self.db.connect() as conn:
            result = self._markup_one(conn, job.target_id, job.params, self._progress(conn, job.id))
        return {"ok": result.ok, "issues": len(result.issues)}

    def _run_markup_book(self, job: Job) -> dict[str, Any]:
        """Разметить главы книги подряд. Уже размеченные — только с ``force``."""
        force = bool(job.params.get("force"))
        with self.db.connect() as conn:
            chapters = [
                c.id for c in repo.list_chapters(conn, job.target_id)
                if force or not repo.list_segments(conn, c.id)
            ]
            repo.update_job(conn, job.id, total=len(chapters), done=0, progress=0.0)

        total = len(chapters)
        done, failed, issues, cancelled = 0, [], 0, False
        for chapter_id in chapters:
            if self._cancelling(job.id) or self._stop.is_set():
                cancelled = True
                break
            with self.db.connect() as conn:
                def report(batch_done: int, batch_total: int, conn=conn) -> None:
                    share = batch_done / batch_total if batch_total else 0.0
                    repo.update_job(conn, job.id, done=done, total=total,
                                    progress=min(1.0, (done + share) / max(total, 1)))
                    conn.commit()

                try:
                    result = self._markup_one(conn, chapter_id, job.params, report)
                    issues += len(result.issues)
                except MarkupConfigError:
                    # Нет ключа или модели — остальные главы упадут так же.
                    # Останавливаемся сразу, а не собираем пятьдесят одинаковых отказов.
                    raise
                except Exception as exc:  # noqa: BLE001 — одна глава не валит книгу
                    log.warning("глава %s не размечена: %s", chapter_id, exc)
                    failed.append({"chapter_id": chapter_id, "error": str(exc)})
            done += 1
            with self.db.connect() as conn:
                repo.update_job(conn, job.id, done=done, total=total,
                                progress=min(1.0, done / max(total, 1)))
        return {
            "chapters": total, "done": done, "failed": failed, "issues": issues,
            "cancelled": cancelled,
            "error": f"глав с ошибкой: {len(failed)}" if failed else None,
        }

    def _chapter(
        self, chapter_id: int, job: Job, parallelism: int, report: Any = None
    ) -> dict[str, Any]:
        """Озвучить главу и склеить её. Продолжение — бесплатно: готовое пропускается."""
        with self.db.connect() as conn:
            outcome = synth.synthesize_chapter(
                conn, chapter_id, parallelism=parallelism,
                force=bool(job.params.get("force")),
                on_progress=report(conn) if report else self._progress(conn, job.id),
                should_stop=lambda: self._stop.is_set() or _is_cancelling(conn, job.id),
            )
            if not outcome["cancelled"]:
                assembled = synth.assemble_chapter(conn, chapter_id)
                outcome["audio"] = assembled.get("path")
                outcome["duration_ms"] = assembled.get("duration_ms")
        return outcome

    def _parallelism(self) -> int:
        with self.db.connect() as conn:
            try:
                return max(1, int(settings(conn).get("synthesis.parallelism") or 1))
            except (TypeError, ValueError):
                return 1

    def _run_chapter(self, job: Job) -> dict[str, Any]:
        return self._chapter(job.target_id, job, self._parallelism())

    def _run_export(self, job: Job) -> dict[str, Any]:
        """Экспорт идёт задачей: m4b длинной книги собирается минутами."""
        kind = (job.params.get("kind") or "m4b").lower()
        handlers = {
            "m4b": export_mod.export_m4b,
            "mp3": export_mod.export_mp3,
            "json": lambda conn, book_id, on_progress=None: export_mod.export_markup(conn, book_id),
        }
        if kind not in handlers:
            raise ValueError(f"неизвестный вид экспорта {kind!r}")
        with self.db.connect() as conn:
            return handlers[kind](conn, job.target_id, on_progress=self._progress(conn, job.id))

    def _run_book(self, job: Job) -> dict[str, Any]:
        with self.db.connect() as conn:
            chapters = [c.id for c in repo.list_chapters(conn, job.target_id)]
        return self._many(job, chapters)

    def _run_folder(self, job: Job) -> dict[str, Any]:
        with self.db.connect() as conn:
            folders = repo.folder_subtree_ids(conn, job.target_id)
            chapters = [
                chapter.id
                for folder_id in folders
                for book in repo.list_books(conn, folder_id)
                for chapter in repo.list_chapters(conn, book.id)
            ]
        return self._many(job, chapters)

    def _many(self, job: Job, chapter_ids: list[int]) -> dict[str, Any]:
        """Озвучить несколько глав подряд, считая прогресс по главам.

        Внутри главы счёт идёт по репликам, снаружи — по главам. Долю пишем
        явно, иначе две единицы измерения перетирали бы друг друга.
        """
        parallelism = self._parallelism()
        total_chapters = len(chapter_ids)
        with self.db.connect() as conn:
            repo.update_job(conn, job.id, total=total_chapters, done=0, progress=0.0)
        done, failed, cancelled = 0, [], False

        def chapter_report(conn):
            def report(segment_done: int, segment_total: int) -> None:
                share = (segment_done / segment_total) if segment_total else 0.0
                repo.update_job(
                    conn, job.id, done=done, total=total_chapters,
                    progress=min(1.0, (done + share) / max(total_chapters, 1)),
                )
                conn.commit()
            return report

        skipped = 0
        for chapter_id in chapter_ids:
            if self._cancelling(job.id) or self._stop.is_set():
                cancelled = True
                break
            with self.db.connect() as conn:
                if not repo.list_segments(conn, chapter_id):
                    # Глава не размечена — озвучивать нечего, это не ошибка книги.
                    skipped += 1
                    done += 1
                    repo.update_job(
                        conn, job.id, done=done, total=total_chapters,
                        progress=min(1.0, done / max(total_chapters, 1)),
                    )
                    continue
            try:
                outcome = self._chapter(chapter_id, job, parallelism, report=chapter_report)
                if outcome.get("cancelled"):
                    cancelled = True
                    break
            except Exception as exc:  # noqa: BLE001 — одна глава не валит книгу
                log.warning("глава %s не озвучена: %s", chapter_id, exc)
                failed.append({"chapter_id": chapter_id, "error": str(exc)})
            done += 1
            with self.db.connect() as conn:
                repo.update_job(
                    conn, job.id, done=done, total=total_chapters,
                    progress=min(1.0, done / max(total_chapters, 1)),
                )
        return {
            "chapters": len(chapter_ids), "done": done, "failed": failed,
            "skipped": skipped, "cancelled": cancelled,
            "error": f"глав с ошибкой: {len(failed)}" if failed else None,
        }


def _is_cancelling(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()
    return bool(row) and row["status"] == CANCELLING


def _default_title(conn: sqlite3.Connection, kind: str, target_id: int | None) -> str:
    """Понятное имя задачи для экрана «Задачи»."""
    try:
        if kind in (KIND_MARKUP, KIND_SYNTHESIS) and target_id:
            chapter = repo.get_chapter(conn, target_id)
            book = repo.get_book(conn, chapter.book_id)
            what = "Разметка" if kind == KIND_MARKUP else "Озвучка"
            return f"{what}: {book.title} — {chapter.label}"
        if kind == KIND_BOOK and target_id:
            return f"Озвучка книги: {repo.get_book(conn, target_id).title}"
        if kind == KIND_FOLDER and target_id:
            return f"Озвучка папки: {repo.get_folder(conn, target_id).name}"
        if kind == KIND_EXPORT and target_id:
            return f"Экспорт: {repo.get_book(conn, target_id).title}"
        if kind == KIND_MARKUP_BOOK and target_id:
            return f"Разметка книги: {repo.get_book(conn, target_id).title}"
    except repo.RepoError:
        pass
    return kind
