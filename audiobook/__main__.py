"""CLI: python -m audiobook <команда> [опции].

Тонкая обёртка над core/ — ровно как api/. Вся логика в core, здесь только
разбор аргументов и печать.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, paths
from .core import checks as checks_mod
from .core import library, repo
from .core.db import Database
from .core.markup import DEFAULT_MODEL, MarkupError
from .core.parser import ParserError


def _setup_console() -> None:
    """Русский текст в консоли Windows без UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _load_dotenv() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    for candidate in (
        find_dotenv(usecwd=True),
        Path(__file__).resolve().parent.parent / ".env",
    ):
        if candidate and Path(candidate).is_file():
            load_dotenv(candidate)
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m audiobook",
        description="Локальный конвертер книг в многоголосые аудиокниги.",
    )
    parser.add_argument("--version", action="version", version=f"audiobook {__version__}")
    parser.add_argument(
        "--library", type=Path, default=None,
        help="корень библиотеки (иначе BOOKTTS_HOME или ./library)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="добавить книгу в библиотеку")
    p_import.add_argument("input", type=Path, help="файл .txt или .epub")
    p_import.add_argument("--title", default="", help="название (иначе из файла)")
    p_import.add_argument("--folder", type=int, default=None, help="id папки")

    sub.add_parser("books", help="что лежит в библиотеке")

    p_chapters = sub.add_parser("chapters", help="главы книги")
    p_chapters.add_argument("book_id", type=int)

    p_markup = sub.add_parser("markup", help="разметить главу по ролям")
    p_markup.add_argument("chapter_id", type=int)
    p_markup.add_argument("--model", default=DEFAULT_MODEL)
    p_markup.add_argument("--base-url", default=None, help="иначе ANTHROPIC_BASE_URL")
    p_markup.add_argument("--batch-chars", type=int, default=3000)
    p_markup.add_argument(
        "--force", action="store_true", help="переразметить, стерев ручные правки"
    )
    p_markup.add_argument("--show", type=int, default=40, help="сколько сегментов печатать")

    p_segments = sub.add_parser("segments", help="показать разметку главы")
    p_segments.add_argument("chapter_id", type=int)
    p_segments.add_argument("--show", type=int, default=0, help="0 — все")

    p_checks = sub.add_parser("checks", help="проверки перед синтезом")
    p_checks.add_argument("chapter_id", type=int)

    p_serve = sub.add_parser("serve", help="веб-интерфейс: редактор разметки")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000, help="0 — любой свободный")
    p_serve.add_argument("--base-url", default=None, help="иначе ANTHROPIC_BASE_URL")
    p_serve.add_argument(
        "--ready-line", action="store_true",
        help="напечатать BOOKTTS_READY {port} — для десктопной оболочки",
    )
    p_serve.add_argument(
        "--exit-with-stdin", action="store_true",
        help="завершиться, когда родитель закроет stdin",
    )

    return parser


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


def cmd_import(args, db: Database) -> int:
    with db.connect() as conn:
        book = library.import_book(
            conn, args.input, folder_id=args.folder, title=args.title
        )
        chapters = repo.list_chapters(conn, book.id)
    print(f"Книга #{book.id}: {book.title}" + (f" — {book.author}" if book.author else ""))
    print(f"Глав: {len(chapters)}")
    for chapter in chapters[:10]:
        print(f"  #{chapter.id:<4} {chapter.label}  —  {len(chapter.text)} симв.")
    if len(chapters) > 10:
        print(f"  … ещё {len(chapters) - 10}")
    if chapters:
        print(f"\nРазметить первую: python -m audiobook markup {chapters[0].id}")
    return 0


def cmd_books(args, db: Database) -> int:
    with db.connect() as conn:
        books = repo.list_books(conn)
        rows = [(b, repo.list_chapters(conn, b.id)) for b in books]
    if not rows:
        print("Библиотека пуста. Добавьте книгу: python -m audiobook import файл.epub")
        return 0
    print(f"Библиотека: {paths.library_root()}\n")
    for book, chapters in rows:
        print(f"  #{book.id:<4} {book.title:<40} глав: {len(chapters)}")
    return 0


def cmd_chapters(args, db: Database) -> int:
    with db.connect() as conn:
        book = repo.get_book(conn, args.book_id)
        chapters = repo.list_chapters(conn, book.id)
        counts = {c.id: len(repo.list_segments(conn, c.id)) for c in chapters}
    print(f"{book.title}\n")
    for chapter in chapters:
        marked = f"сегментов: {counts[chapter.id]}" if counts[chapter.id] else "не размечена"
        print(f"  #{chapter.id:<4} {chapter.label:<40} {len(chapter.text):>6} симв.  {marked}")
    return 0


def _print_segments(segments, limit: int = 0) -> None:
    shown = segments if not limit else segments[:limit]
    for segment in shown:
        manual = " ✎" if segment.is_manual else ""
        emotion = "" if segment.emotion == "нейтрально" else f" ({segment.emotion})"
        error = f"  ⚠ {segment.error}" if segment.error else ""
        print(f"  [{segment.order:>3}] {segment.speaker}{emotion}{manual}: {segment.text}{error}")
    if limit and len(segments) > limit:
        print(f"  … ещё {len(segments) - limit} сегментов")


def cmd_markup(args, db: Database) -> int:
    with db.connect() as conn:
        chapter = repo.get_chapter(conn, args.chapter_id)
        print(f"Размечаю: {chapter.label} ({len(chapter.text)} симв.)")

        def progress(done: int, total: int) -> None:
            print(f"  батч {done}/{total}", end="\r", file=sys.stderr)

        result = library.markup_chapter(
            conn,
            chapter.id,
            model=args.model,
            base_url=args.base_url,
            batch_chars=args.batch_chars,
            force=args.force,
            on_progress=progress,
        )
        segments = repo.list_segments(conn, chapter.id)
        speakers = repo.chapter_speakers(conn, chapter.id)

    print(" " * 40, end="\r", file=sys.stderr)
    print()
    _print_segments(segments, args.show)
    print()
    print(f"Сегментов: {len(segments)}, персонажей: {len(speakers)}")
    print("Персонажи: " + ", ".join(f"{n} ({c})" for n, c in speakers.items()))
    if result.usage:
        tokens = result.usage.get("input_tokens", 0) + result.usage.get("output_tokens", 0)
        print(f"Токенов: {tokens}")
    for issue in result.issues:
        where = f" (абзацы {issue.paragraphs})" if issue.paragraphs else ""
        print(f"  ⚠ {issue.kind}: {issue.detail}{where}")
    return 0 if result.ok else 1


def cmd_segments(args, db: Database) -> int:
    with db.connect() as conn:
        chapter = repo.get_chapter(conn, args.chapter_id)
        segments = repo.list_segments(conn, chapter.id)
        speakers = repo.chapter_speakers(conn, chapter.id)
    if not segments:
        print(f"{chapter.label}: не размечена.")
        print(f"Разметить: python -m audiobook markup {chapter.id}")
        return 1
    print(f"{chapter.label}\n")
    _print_segments(segments, args.show)
    print()
    print("Персонажи: " + ", ".join(f"{n} ({c})" for n, c in speakers.items()))
    return 0


def cmd_checks(args, db: Database) -> int:
    with db.connect() as conn:
        chapter = repo.get_chapter(conn, args.chapter_id)
        findings = checks_mod.check_chapter(conn, chapter.id)
    print(f"{chapter.label}\n")
    if not findings:
        print("Замечаний нет — можно синтезировать.")
        return 0
    for finding in findings:
        mark = "✖" if finding.severity == "blocker" else "⚠"
        print(f"  {mark} {finding.message}")
    blockers = sum(1 for f in findings if f.severity == "blocker")
    print(f"\nБлокирующих: {blockers}, предупреждений: {len(findings) - blockers}")
    return 1 if blockers else 0


def cmd_serve(args, db: Database) -> int:
    import os

    from .api.app import TOKEN_ENV, serve

    serve(
        host=args.host,
        port=args.port,
        db=db,
        base_url=args.base_url,
        # Токен — только из окружения: аргументы процесса видны всем.
        token=os.environ.get(TOKEN_ENV) or None,
        announce=args.ready_line,
        exit_with_stdin=args.exit_with_stdin,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    _load_dotenv()
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.library:
        paths.set_library_root(args.library)
    paths.ensure_layout()
    db = Database().setup()

    handlers = {
        "import": cmd_import,
        "books": cmd_books,
        "chapters": cmd_chapters,
        "markup": cmd_markup,
        "segments": cmd_segments,
        "checks": cmd_checks,
        "serve": cmd_serve,
    }
    try:
        return handlers[args.command](args, db)
    except (ParserError, MarkupError, repo.RepoError, library.LibraryError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано. Сделанное сохранено в базе.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
