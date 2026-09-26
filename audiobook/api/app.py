"""HTTP-слой. Ничего не решает сам — только разбирает запрос, зовёт core
и сериализует ответ.

Никакого состояния в памяти: на каждый запрос открывается соединение с базой
и закрывается по его завершении.
"""

# Без ``from __future__ import annotations``: FastAPI разрешает аннотации
# обработчиков через globals() модуля, а модели запросов объявлены внутри
# create_app. Со строковыми аннотациями body-модель принимается за query.

import hmac
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from .. import paths
from ..core import bookinfo, catalog
from ..core import checks as checks_mod
from ..core import editing, jobs, library, repo, secrets, synth
from ..core import export as export_mod
from ..core.export import ExportError
from ..core.db import Database
from ..core.engines import EngineError
from ..core.markup import DEFAULT_MODEL, MarkupConfigError, MarkupError
from ..core.models import EMOTIONS, NARRATOR
from ..core.parser import SUPPORTED_EXTENSIONS, ParserError

log = logging.getLogger("audiobook.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Различимые оттенки для персонажей. Индекс — от хеша имени, чтобы цвет
# не менялся между открытиями главы.
# Оттенки подобраны так, чтобы читаться и на светлой, и на тёмной теме и не
# спорить с оранжевым акцентом интерфейса: его цвет занят кнопками.
PALETTE = (
    "#4f76d6", "#2a9d8f", "#8b5cf6", "#d05a4a", "#c9a227",
    "#3ba3d8", "#d0559e", "#6a9a2f", "#9b6bd6", "#43a047",
    "#7a8596", "#b4713a",
)
NARRATOR_COLOR = "#7d766e"

# Десктоп запускает сервер с токеном: без него на 127.0.0.1 мог бы постучаться
# любой локальный процесс или страница в браузере. Токен приходит один раз
# в /desktop/enter и дальше живёт в HttpOnly-куке.
TOKEN_ENV = "BOOKTTS_TOKEN"
TOKEN_COOKIE = "booktts_token"
TOKEN_HEADER = "x-booktts-token"
# Изменяющие запросы обязаны нести этот заголовок. Чужая страница не может
# добавить его без CORS-preflight, а preflight мы не разрешаем.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "booktts"
READY_PREFIX = "BOOKTTS_READY "


# Для ошибок проверки запроса: имя поля и суть ошибки по-русски.
FIELD_NAMES = {
    "title": "название", "author": "автор", "year": "год", "country": "страна",
    "text": "текст", "name": "имя", "path": "путь к файлу", "key": "ключ",
    "speaker": "роль", "term": "слово", "offset": "место в тексте",
}
VALIDATION_TEXT = {
    "missing": "не заполнено",
    "string_too_short": "не заполнено",
    "string_too_long": "слишком длинно",
    "int_parsing": "нужно целое число",
    "int_type": "нужно целое число",
    "float_parsing": "нужно число",
    "greater_than_equal": "слишком маленькое значение",
    "less_than_equal": "слишком большое значение",
    "json_invalid": "запрос не разобрать",
    "list_type": "нужен список",
    "string_type": "нужен текст",
}


def color_for(speaker: str) -> str:
    import hashlib

    if speaker == NARRATOR:
        return NARRATOR_COLOR
    digest = hashlib.sha1(speaker.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def create_app(
    db: Database | None = None,
    base_url: str | None = None,
    client: Any = None,
    token: str | None = None,
):
    """Собрать приложение. ``db`` и ``client`` подменяются в тестах.

    ``token`` — пропуск для десктопа; без него сервер открыт, как в режиме
    разработки через ``python -m audiobook serve``.
    """
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "для веб-интерфейса нужны fastapi и uvicorn: pip install -r requirements.txt"
        ) from exc

    class ImportRequest(BaseModel):
        path: str = Field(min_length=1)
        title: str = ""
        folder_id: int | None = None

    class MarkupRequest(BaseModel):
        model: str = ""  # пусто — модель из настроек
        batch_chars: int = Field(default=3000, ge=500, le=20000)
        force: bool = False

    class AssignRequest(BaseModel):
        segment_ids: list[int] = Field(default_factory=list)
        speaker: str = ""

    class RangeRequest(BaseModel):
        char_start: int = Field(ge=0)
        char_end: int = Field(ge=0)
        speaker: str = ""

    class SplitRequest(BaseModel):
        segment_id: int
        offset: int = Field(ge=0)

    class MergeRequest(BaseModel):
        segment_ids: list[int] = Field(default_factory=list)

    class RenameRequest(BaseModel):
        old: str
        new: str
        whole_book: bool = False

    class EmotionRequest(BaseModel):
        segment_ids: list[int] = Field(default_factory=list)
        emotion: str = EMOTIONS[0]

    class TextRequest(BaseModel):
        segment_id: int
        text: str

    class FolderRequest(BaseModel):
        name: str = Field(min_length=1)
        parent_id: int | None = None

    class NameRequest(BaseModel):
        name: str = Field(min_length=1)

    class MoveRequest(BaseModel):
        parent_id: int | None = None  # папка-родитель; None — корень
        position: int | None = Field(default=None, ge=0)

    class BookPatch(BaseModel):
        title: str | None = None
        author: str | None = None
        year: int | None = None
        country: str | None = None
        description: str | None = None
        genres: list[str] | None = None
        moods: list[str] | None = None
        tags: list[str] | None = None
        info_source: str | None = None  # "ai" — сведения подобрала нейросеть

    class DescribeRequest(BaseModel):
        title: str = Field(min_length=1)
        author: str = ""
        year: int | None = None
        country: str = ""

    class ChapterFileRequest(BaseModel):
        path: str = Field(min_length=1)
        after: int | None = None  # номер главы, после которой вставить; None — в конец

    class RoleColorRequest(BaseModel):
        speaker: str = Field(min_length=1)
        slot: int = Field(ge=0, le=6)

    class QwenMoveRequest(BaseModel):
        target: str = Field(min_length=1)

    class ListenedRequest(BaseModel):
        listened: bool = True

    class ChapterTextRequest(BaseModel):
        text: str = ""
        title: str = ""
        after: int | None = None

    class ChapterPatch(BaseModel):
        title: str

    class SplitChapterRequest(BaseModel):
        offset: int = Field(ge=0)
        title: str = ""

    class ClientLog(BaseModel):
        message: str = ""
        source: str = ""
        line: int = 0
        stack: str = ""

    class RefreshRequest(BaseModel):
        engine: str = ""

    class PreviewRequest(BaseModel):
        text: str = ""
        rate: float = Field(default=1.0, ge=0.5, le=2.0)
        pitch: float = Field(default=1.0, ge=0.5, le=2.0)
        volume: float = Field(default=1.0, ge=0.1, le=2.0)

    class CastRequest(BaseModel):
        speaker: str = Field(min_length=1)
        voice_id: int | None = None
        rate: float = Field(default=1.0, ge=0.5, le=2.0)
        pitch: float = Field(default=1.0, ge=0.5, le=2.0)
        volume: float = Field(default=1.0, ge=0.1, le=2.0)

    class AutoCastRequest(BaseModel):
        engine: str = "silero"
        overwrite: bool = False

    class ProfileRequest(BaseModel):
        name: str = Field(min_length=1)

    class ApplyProfileRequest(BaseModel):
        profile_id: int
        overwrite: bool = True

    class SettingsRequest(BaseModel):
        values: dict[str, Any] = Field(default_factory=dict)

    class TokenRequest(BaseModel):
        key: str = Field(min_length=1)

    class JobRequest(BaseModel):
        force: bool = False

    class ExportRequest(BaseModel):
        kind: str = "m4b"  # m4b | mp3 | json

    class PlaybackRequest(BaseModel):
        chapter_id: int | None = None
        position_ms: int = Field(default=0, ge=0)

    class QueueRequest(BaseModel):
        chapter_ids: list[int] = Field(default_factory=list)

    class PronunciationRequest(BaseModel):
        term: str = Field(min_length=1)
        replacement: str = ""
        whole_word: bool = True
        case_sensitive: bool = False

    class ScopedPronunciationRequest(PronunciationRequest):
        book_id: int | None = None  # None — правило для всех книг

    app = FastAPI(title="BookTTS", docs_url="/api/docs", redoc_url=None)
    app.state.db = db or Database().setup()
    app.state.base_url = base_url
    app.state.client = client
    app.state.token = token or None
    # Очередь создаётся всегда, но нить запускает только serve(): в тестах
    # задачи кладутся в базу и выполняются явно.
    app.state.queue = jobs.JobQueue(app.state.db, client=client, base_url=base_url)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        expected = app.state.token
        if expected and request.url.path != "/desktop/enter":
            supplied = request.cookies.get(TOKEN_COOKIE) or request.headers.get(TOKEN_HEADER) or ""
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JSONResponse(status_code=401, content={"detail": "нет доступа"})
        if request.method not in ("GET", "HEAD", "OPTIONS") and (
            request.headers.get(CSRF_HEADER) != CSRF_VALUE
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": f"изменяющий запрос без заголовка {CSRF_HEADER}"},
            )
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            # WebView2 держит кеш между запусками — без этого правки интерфейса не видны.
            response.headers["cache-control"] = "no-cache"
        return response

    @app.get("/desktop/enter", include_in_schema=False)
    def desktop_enter(token: str = ""):
        """Точка входа окна Tauri: обменять токен из URL на куку."""
        expected = app.state.token
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(401, "нет доступа")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(TOKEN_COOKIE, expected, httponly=True, samesite="strict")
        return response

    def session(x_session: str = Header(default="default")) -> str:
        """Сессия редактирования — по ней живёт история отмены."""
        return x_session or "default"

    @app.exception_handler(repo.RepoError)
    def _not_found(request, exc):  # noqa: ARG001 — сигнатура задана FastAPI
        """Нет такой книги, главы или сегмента — это 404, а не падение."""
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # Обработчик выбирается по MRO: InvalidOperation точнее, чем RepoError.
    @app.exception_handler(repo.InvalidOperation)
    @app.exception_handler(library.LibraryError)
    @app.exception_handler(ParserError)
    @app.exception_handler(catalog.CatalogError)
    @app.exception_handler(secrets.SecretsError)
    @app.exception_handler(EngineError)
    @app.exception_handler(ExportError)
    @app.exception_handler(bookinfo.BookInfoError)
    def _bad_request(request, exc):  # noqa: ARG001
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    def _invalid(request, exc):  # noqa: ARG001
        """Ошибки проверки запроса по-русски: «Field required» ничего не скажет."""
        parts = []
        for error in exc.errors():
            loc = [str(x) for x in error.get("loc", ()) if x not in ("body", "query", "path")]
            field = FIELD_NAMES.get(loc[-1], loc[-1]) if loc else "запрос"
            parts.append(f"{field} — {VALIDATION_TEXT.get(error.get('type', ''), error.get('msg', ''))}")
        return JSONResponse(status_code=422, content={"detail": "проверьте данные: " + "; ".join(parts)})

    @app.exception_handler(Exception)
    def _crash(request, exc):  # noqa: ARG001
        """Непредвиденная ошибка — понятный ответ вместо «Internal Server Error»."""
        log.exception("ошибка при обработке %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"внутренняя ошибка ({type(exc).__name__}): {exc}"[:500]},
        )

    def _view(conn, chapter_id: int) -> dict:
        data = library.chapter_view(conn, chapter_id)
        for segment in data["segments"]:
            segment["color"] = color_for(segment["speaker"])
        for speaker in data["speakers"]:
            speaker["color"] = color_for(speaker["name"])
        return data

    def _edit_response(conn, chapter_id: int, result) -> dict:
        payload = _view(conn, chapter_id)
        payload["history"] = {
            "can_undo": result.can_undo,
            "can_redo": result.can_redo,
            "description": result.description,
        }
        return payload

    # ---------------- страница ----------------

    @app.get("/", include_in_schema=False)
    def index():
        page = WEB_DIR / "index.html"
        if not page.is_file():  # pragma: no cover
            raise HTTPException(500, f"не найдена страница {page}")
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.get("/api/config")
    def config():
        models: list[str] = []
        error = ""
        with app.state.db.connect() as conn:
            connection = library.markup_connection(conn, base_url=app.state.base_url)
        try:
            import anthropic

            options = {k: v for k, v in connection.items() if k != "model" and v}
            probe = app.state.client or anthropic.Anthropic(**options)
            models = [m.id for m in probe.models.list()]
        except Exception as exc:  # noqa: BLE001 — список моделей не критичен
            error = str(exc)[:200]
        from .. import __version__

        return {
            "version": __version__,
            "default_model": connection["model"],
            "models": models,
            "models_error": error,
            "emotions": list(EMOTIONS),
            "narrator": NARRATOR,
            "library": str(paths.library_root()),
            "endpoint": connection["base_url"]
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "api.anthropic.com",
        }

    # ---------------- библиотека ----------------

    @app.get("/api/library")
    def library_tree():
        with app.state.db.connect() as conn:
            return library.library_tree(conn)

    @app.post("/api/client-log", status_code=204)
    def client_log(entry: ClientLog):
        """Ошибка в интерфейсе — в stderr бэкенда, откуда её печатает оболочка."""
        from fastapi import Response

        log.warning(
            "интерфейс: %s (%s:%s)\n%s",
            entry.message[:2000], entry.source[:500], entry.line, entry.stack[:4000],
        )
        return Response(status_code=204)

    @app.get("/api/formats")
    def formats():
        return {"extensions": list(SUPPORTED_EXTENSIONS)}

    @app.get("/api/search")
    def search(q: str = "", limit: int = 40):
        with app.state.db.connect() as conn:
            return {"query": q, "results": library.search_library(conn, q, limit=min(limit, 200))}

    @app.post("/api/folders")
    def create_folder(request: FolderRequest):
        with app.state.db.connect() as conn:
            return repo.create_folder(conn, request.name, request.parent_id).to_dict()

    @app.patch("/api/folders/{folder_id}")
    def rename_folder(folder_id: int, request: NameRequest):
        with app.state.db.connect() as conn:
            return repo.rename_folder(conn, folder_id, request.name).to_dict()

    @app.post("/api/folders/{folder_id}/move")
    def move_folder(folder_id: int, request: MoveRequest):
        with app.state.db.connect() as conn:
            return repo.move_folder(conn, folder_id, request.parent_id, request.position).to_dict()

    @app.delete("/api/folders/{folder_id}")
    def delete_folder(folder_id: int):
        with app.state.db.connect() as conn:
            repo.delete_folder(conn, folder_id)
            return {"deleted": folder_id}

    @app.get("/api/books/{book_id}")
    def book(book_id: int):
        with app.state.db.connect() as conn:
            return library.book_view(conn, book_id)

    @app.patch("/api/books/{book_id}")
    def update_book(book_id: int, request: BookPatch):
        # Только присланные поля: так год можно и стереть (null), и не трогать.
        fields = request.model_dump(exclude_unset=True)
        if fields.get("title") is None:
            fields.pop("title", None)
        with app.state.db.connect() as conn:
            return repo.update_book(conn, book_id, **fields).to_dict()

    @app.post("/api/books/{book_id}/move")
    def move_book(book_id: int, request: MoveRequest):
        with app.state.db.connect() as conn:
            return repo.move_book(conn, book_id, request.parent_id, request.position).to_dict()

    @app.delete("/api/books/{book_id}")
    def delete_book(book_id: int):
        with app.state.db.connect() as conn:
            library.delete_book(conn, book_id)
            return {"deleted": book_id}

    @app.get("/api/books/{book_id}/cover")
    def book_cover(book_id: int):
        with app.state.db.connect() as conn:
            book = repo.get_book(conn, book_id)
        if not book.cover_path:
            raise HTTPException(404, "у книги нет обложки")
        try:
            cover = paths.absolute(book.cover_path)
        except paths.PathsError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not cover.is_file():
            raise HTTPException(404, "файл обложки не найден")
        return FileResponse(cover)

    @app.put("/api/books/{book_id}/cover")
    async def upload_cover(book_id: int, request: Request):
        """Своя обложка: тело запроса — картинка."""
        data = await request.body()
        with app.state.db.connect() as conn:
            return library.set_cover(conn, book_id, data).to_dict()

    @app.delete("/api/books/{book_id}/cover")
    def delete_cover(book_id: int):
        with app.state.db.connect() as conn:
            return library.remove_cover(conn, book_id).to_dict()

    @app.get("/api/engines/qwen/home")
    def qwen_home(size: bool = False):
        """Где лежит Qwen и куда его лучше перенести."""
        import shutil as _shutil

        from ..core.engines import qwen as qwen_engine

        with app.state.db.connect() as conn:
            options = catalog.settings(conn)
        engine = qwen_engine.engine(options)
        beside = qwen_engine.suggested_home()
        free = None
        if beside is not None:
            try:
                free = _shutil.disk_usage(qwen_engine._existing_parent(beside)).free
            except OSError:
                free = None
        return {
            "home": str(engine.home),
            "installed": engine.installed(),
            "size": qwen_engine.folder_size(engine.home) if size and engine.installed() else None,
            "suggested": str(beside) if beside else "",
            "suggested_free": free,
            "program_dir": str(qwen_engine.program_dir() or ""),
            "same_drive": bool(beside) and Path(engine.home).anchor.lower() == Path(beside).anchor.lower(),
        }

    @app.post("/api/engines/qwen/move")
    def qwen_move(request: QwenMoveRequest):
        from ..core.engines import qwen as qwen_engine

        with app.state.db.connect() as conn:
            options = catalog.settings(conn)
        source = qwen_engine.engine(options).home
        target = Path(request.target).expanduser()
        if not target.is_absolute():
            raise HTTPException(400, "укажите полный путь, например E:\\Programs\\BookTTS\\qwen")
        job = app.state.queue.enqueue(
            jobs.KIND_QWEN_MOVE, None, title=f"Перенос Qwen3-TTS в {target}",
            params={"source": str(source), "target": str(target)},
        )
        return job.to_dict()

    @app.put("/api/books/{book_id}/role-color")
    def role_color(book_id: int, request: RoleColorRequest):
        """Свой цвет роли — вместо выданного автоматически."""
        with app.state.db.connect() as conn:
            return {"slots": repo.set_role_slot(conn, book_id, request.speaker, request.slot)}

    @app.get("/api/book-vocabulary")
    def book_vocabulary():
        return bookinfo.vocabulary()

    @app.post("/api/books/{book_id}/describe")
    def describe_book(book_id: int, request: DescribeRequest):
        """Подобрать жанры, настроение, метки и описание через Claude. Ничего не сохраняет."""
        with app.state.db.connect() as conn:
            return bookinfo.suggest_info(
                conn, book_id, title=request.title, author=request.author,
                year=request.year, country=request.country,
                client=app.state.client, base_url=app.state.base_url,
            )

    def _chapters_payload(conn, created) -> dict:
        return {"chapters": [c.to_dict(with_text=False) for c in created]}

    @app.post("/api/books/{book_id}/chapters/import")
    def add_chapters_file(book_id: int, request: ChapterFileRequest):
        source = Path(request.path).expanduser()
        if not source.is_file():
            raise HTTPException(400, f"файл не найден: {source}")
        with app.state.db.connect() as conn:
            return _chapters_payload(conn, library.add_chapters_from_file(conn, book_id, source, request.after))

    @app.put("/api/books/{book_id}/chapters/upload")
    async def add_chapters_upload(book_id: int, request: Request, filename: str, after: int | None = None):
        """То же из браузера или перетаскиванием: тело — сам файл."""
        from starlette.concurrency import run_in_threadpool

        name = Path(filename).name
        if not name or not name.lower().endswith(SUPPORTED_EXTENSIONS):
            raise HTTPException(400, f"поддерживаются: {', '.join(SUPPORTED_EXTENSIONS)}")
        data = await request.body()
        if not data:
            raise HTTPException(400, "пустой файл")

        def store_and_add() -> dict:
            uploads = paths.cache_dir() / "uploads"
            uploads.mkdir(parents=True, exist_ok=True)
            temporary = uploads / name
            temporary.write_bytes(data)
            try:
                with app.state.db.connect() as conn:
                    return _chapters_payload(conn, library.add_chapters_from_file(conn, book_id, temporary, after))
            finally:
                temporary.unlink(missing_ok=True)

        return await run_in_threadpool(store_and_add)

    @app.post("/api/books/{book_id}/chapters")
    def add_chapter_text(book_id: int, request: ChapterTextRequest):
        with app.state.db.connect() as conn:
            created = library.add_chapter_from_text(conn, book_id, request.text, request.title, request.after)
            return _chapters_payload(conn, [created])

    @app.put("/api/chapters/{chapter_id}/listened")
    def mark_listened(chapter_id: int, request: ListenedRequest):
        with app.state.db.connect() as conn:
            return repo.set_chapter_listened(conn, chapter_id, request.listened).to_dict(with_text=False)

    @app.delete("/api/chapters/{chapter_id}")
    def delete_chapter(chapter_id: int):
        with app.state.db.connect() as conn:
            library.remove_chapter(conn, chapter_id)
            return {"deleted": chapter_id}

    @app.put("/api/books/upload")
    async def upload_book(request: Request, filename: str, folder_id: int | None = None):
        """Импорт из браузера: тело запроса — сам файл книги.

        В десктопе путь выбирается системным диалогом и идёт в /import; этот
        маршрут — для режима разработки в обычном браузере.
        """
        from starlette.concurrency import run_in_threadpool

        name = Path(filename).name
        if not name or not name.lower().endswith(SUPPORTED_EXTENSIONS):
            raise HTTPException(400, f"поддерживаются: {', '.join(SUPPORTED_EXTENSIONS)}")
        data = await request.body()
        if not data:
            raise HTTPException(400, "пустой файл")

        def store_and_import() -> dict:
            uploads = paths.cache_dir() / "uploads"
            uploads.mkdir(parents=True, exist_ok=True)
            temporary = uploads / name
            temporary.write_bytes(data)
            try:
                with app.state.db.connect() as conn:
                    imported = library.import_book(conn, temporary, folder_id=folder_id)
                    return {
                        **imported.to_dict(),
                        "chapters": [c.to_dict(with_text=False) for c in repo.list_chapters(conn, imported.id)],
                    }
            finally:
                temporary.unlink(missing_ok=True)

        return await run_in_threadpool(store_and_import)

    @app.patch("/api/chapters/{chapter_id}")
    def rename_chapter(chapter_id: int, request: ChapterPatch):
        with app.state.db.connect() as conn:
            return repo.update_chapter(conn, chapter_id, title=request.title.strip()).to_dict(with_text=False)

    # Не /split: так называется разделение сегмента в редакторе разметки.
    @app.post("/api/chapters/{chapter_id}/split-chapter")
    def split_chapter(chapter_id: int, request: SplitChapterRequest):
        with app.state.db.connect() as conn:
            head, tail = library.split_chapter(conn, chapter_id, request.offset, request.title)
            return {"chapters": [head.to_dict(with_text=False), tail.to_dict(with_text=False)]}

    @app.post("/api/chapters/{chapter_id}/merge-next")
    def merge_next(chapter_id: int):
        with app.state.db.connect() as conn:
            return library.merge_with_next(conn, chapter_id).to_dict(with_text=False)

    @app.post("/api/books/import")
    def import_book(request: ImportRequest):
        source = Path(request.path).expanduser()
        if not source.is_file():
            raise HTTPException(400, f"файл не найден: {source}")
        with app.state.db.connect() as conn:
            try:
                book = library.import_book(
                    conn, source, folder_id=request.folder_id, title=request.title
                )
            except Exception as exc:  # noqa: BLE001 — разбор книги
                raise HTTPException(400, str(exc)) from exc
            return {
                **book.to_dict(),
                "chapters": [c.to_dict(with_text=False) for c in repo.list_chapters(conn, book.id)],
            }

    @app.get("/api/books/{book_id}/chapters")
    def book_chapters(book_id: int):
        with app.state.db.connect() as conn:
            chapters = repo.list_chapters(conn, book_id)
            return {
                "book": repo.get_book(conn, book_id).to_dict(),
                "chapters": [
                    {
                        **c.to_dict(with_text=False),
                        "segments": len(repo.list_segments(conn, c.id)),
                    }
                    for c in chapters
                ],
            }

    # ---------------- глава ----------------

    @app.get("/api/chapters/{chapter_id}")
    def chapter(chapter_id: int, session_id: str = Depends(session)):
        with app.state.db.connect() as conn:
            payload = _view(conn, chapter_id)
            payload["history"] = editing.history_state(conn, session_id, chapter_id)
            return payload

    @app.post("/api/chapters/{chapter_id}/markup")
    def markup(chapter_id: int, request: MarkupRequest):
        with app.state.db.connect() as conn:
            try:
                result = library.markup_chapter(
                    conn,
                    chapter_id,
                    client=app.state.client,
                    model=request.model,
                    base_url=app.state.base_url,
                    batch_chars=request.batch_chars,
                    force=request.force,
                )
            except MarkupConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
            except MarkupError as exc:  # pragma: no cover
                raise HTTPException(502, str(exc)) from exc
            payload = _view(conn, chapter_id)
            payload["markup"] = {
                "ok": result.ok,
                "issues": [i.to_dict() for i in result.issues],
                "usage": result.usage,
                "model": result.model,
            }
            return payload

    @app.get("/api/chapters/{chapter_id}/checks")
    def chapter_checks(chapter_id: int):
        with app.state.db.connect() as conn:
            findings = checks_mod.check_chapter(conn, chapter_id)
            return {
                "findings": [f.to_dict() for f in findings],
                "blockers": sum(1 for f in findings if f.severity == "blocker"),
            }

    # ---------------- правки ----------------

    def _edit(chapter_id: int, session_id: str, operation):
        with app.state.db.connect() as conn:
            try:
                result = operation(conn)
            except editing.EditError as exc:
                raise HTTPException(400, str(exc)) from exc
            except repo.RepoError as exc:
                raise HTTPException(404, str(exc)) from exc
            return _edit_response(conn, chapter_id, result)

    @app.post("/api/chapters/{chapter_id}/assign")
    def assign(chapter_id: int, request: AssignRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.assign_speaker(
                conn, session_id, chapter_id, request.segment_ids, request.speaker
            ),
        )

    @app.post("/api/chapters/{chapter_id}/assign-range")
    def assign_range(chapter_id: int, request: RangeRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.assign_range(
                conn, session_id, chapter_id,
                request.char_start, request.char_end, request.speaker,
            ),
        )

    @app.post("/api/chapters/{chapter_id}/split")
    def split(chapter_id: int, request: SplitRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.split_segment(
                conn, session_id, chapter_id, request.segment_id, request.offset
            ),
        )

    @app.post("/api/chapters/{chapter_id}/merge")
    def merge(chapter_id: int, request: MergeRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.merge_segments(
                conn, session_id, chapter_id, request.segment_ids
            ),
        )

    @app.post("/api/chapters/{chapter_id}/rename")
    def rename(chapter_id: int, request: RenameRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.rename_speaker(
                conn, session_id, chapter_id,
                request.old, request.new, whole_book=request.whole_book,
            ),
        )

    @app.post("/api/chapters/{chapter_id}/emotion")
    def emotion(chapter_id: int, request: EmotionRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.set_emotion(
                conn, session_id, chapter_id, request.segment_ids, request.emotion
            ),
        )

    @app.post("/api/chapters/{chapter_id}/text")
    def set_text(chapter_id: int, request: TextRequest, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.set_text(
                conn, session_id, chapter_id, request.segment_id, request.text
            ),
        )

    @app.post("/api/chapters/{chapter_id}/undo")
    def undo(chapter_id: int, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.undo(conn, session_id, chapter_id),
        )

    @app.post("/api/chapters/{chapter_id}/redo")
    def redo(chapter_id: int, session_id: str = Depends(session)):
        return _edit(
            chapter_id, session_id,
            lambda conn: editing.redo(conn, session_id, chapter_id),
        )

    # ---------------- голоса ----------------

    @app.get("/api/engines")
    def engines():
        with app.state.db.connect() as conn:
            return {"engines": catalog.engines_status(conn), "settings": catalog.settings(conn)}

    @app.post("/api/engines/refresh")
    def refresh_engines(request: RefreshRequest):
        with app.state.db.connect() as conn:
            return {"report": catalog.refresh(conn, request.engine or None)}

    @app.get("/api/voices")
    def voices(
        engine: str = "", gender: str = "", language: str = "", q: str = "",
        all_voices: bool = False,
    ):
        with app.state.db.connect() as conn:
            return {"voices": catalog.catalog(
                conn, engine=engine or None, gender=gender or None,
                language=language or None, search=q or None, available_only=not all_voices,
            )}

    @app.post("/api/voices/previews")
    def prepare_previews():
        """Поставить в очередь образцы голосов, которых ещё нет. Уже идёт — не дублировать."""
        with app.state.db.connect() as conn:
            missing = len(catalog.missing_previews(conn))
            running = next((j for j in repo.list_jobs(conn, active_only=True) if j.kind == jobs.KIND_PREVIEWS), None)
        if running:
            return {"job": running.to_dict(), "missing": missing}
        if not missing:
            return {"job": None, "missing": 0}
        job = app.state.queue.enqueue(jobs.KIND_PREVIEWS, None, title=f"Образцы голосов ({missing})")
        return {"job": job.to_dict(), "missing": missing}

    @app.post("/api/voices/{voice_id}/preview")
    def preview_voice(voice_id: int, request: PreviewRequest):
        with app.state.db.connect() as conn:
            return catalog.preview(
                conn, voice_id, text=request.text,
                rate=request.rate, pitch=request.pitch, volume=request.volume,
            )

    @app.get("/api/books/{book_id}/cast")
    def book_cast(book_id: int):
        with app.state.db.connect() as conn:
            return catalog.cast_view(conn, book_id)

    @app.put("/api/books/{book_id}/cast")
    def set_book_cast(book_id: int, request: CastRequest):
        with app.state.db.connect() as conn:
            catalog.assign(
                conn, book_id, request.speaker, request.voice_id,
                request.rate, request.pitch, request.volume,
            )
            return catalog.cast_view(conn, book_id)

    @app.post("/api/books/{book_id}/cast/auto")
    def auto_cast(book_id: int, request: AutoCastRequest):
        with app.state.db.connect() as conn:
            assigned = catalog.auto_assign(
                conn, book_id, engine=request.engine, overwrite=request.overwrite
            )
            return {"assigned": assigned, **catalog.cast_view(conn, book_id)}

    # ---------------- профили озвучки ----------------

    @app.get("/api/profiles")
    def profiles():
        with app.state.db.connect() as conn:
            return {"profiles": repo.list_profiles(conn)}

    @app.post("/api/books/{book_id}/profiles")
    def save_profile(book_id: int, request: ProfileRequest):
        with app.state.db.connect() as conn:
            return catalog.save_profile(conn, book_id, request.name)

    @app.post("/api/books/{book_id}/apply-profile")
    def apply_profile(book_id: int, request: ApplyProfileRequest):
        with app.state.db.connect() as conn:
            return catalog.apply_profile(
                conn, book_id, request.profile_id, overwrite=request.overwrite
            )

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: int):
        with app.state.db.connect() as conn:
            repo.delete_profile(conn, profile_id)
            return {"deleted": profile_id}

    # ---------------- настройки и ключи ----------------

    @app.get("/api/settings")
    def get_settings():
        with app.state.db.connect() as conn:
            return {
                "settings": catalog.settings(conn),
                "defaults": catalog.DEFAULT_SETTINGS,
                "choices": catalog.CHOICES,
            }

    @app.put("/api/settings")
    def put_settings(request: SettingsRequest):
        with app.state.db.connect() as conn:
            return {"settings": catalog.save_settings(conn, request.values)}

    @app.get("/api/tokens")
    def tokens():
        return {"tokens": secrets.status()}

    @app.put("/api/tokens/{service}")
    def put_token(service: str, request: TokenRequest):
        secrets.set_key(service, request.key)
        return {"tokens": secrets.status()}

    @app.delete("/api/tokens/{service}")
    def delete_token(service: str):
        secrets.delete_key(service)
        return {"tokens": secrets.status()}

    @app.post("/api/tokens/{service}/from-env")
    def import_token(service: str):
        if not secrets.import_from_env(service):
            raise HTTPException(400, "в окружении этого ключа нет")
        return {"tokens": secrets.status()}

    # ---------------- озвучка и очередь задач ----------------

    def _enqueue(kind: str, target_id: int, force: bool) -> dict:
        job = app.state.queue.enqueue(kind, target_id, params={"force": force})
        return job.to_dict()

    @app.post("/api/chapters/{chapter_id}/synthesize")
    def synthesize_chapter(chapter_id: int, request: JobRequest):
        with app.state.db.connect() as conn:
            prepared = synth.plan(conn, chapter_id)
            if prepared["missing_voice"]:
                raise HTTPException(
                    400, "нет голоса у: " + ", ".join(prepared["missing_voice"])
                )
        return _enqueue(jobs.KIND_SYNTHESIS, chapter_id, request.force)

    @app.post("/api/books/{book_id}/synthesize")
    def synthesize_book(book_id: int, request: JobRequest):
        with app.state.db.connect() as conn:
            repo.get_book(conn, book_id)
        return _enqueue(jobs.KIND_BOOK, book_id, request.force)

    @app.post("/api/folders/{folder_id}/synthesize")
    def synthesize_folder(folder_id: int, request: JobRequest):
        with app.state.db.connect() as conn:
            repo.get_folder(conn, folder_id)
        return _enqueue(jobs.KIND_FOLDER, folder_id, request.force)

    @app.post("/api/chapters/{chapter_id}/markup-job")
    def markup_job(chapter_id: int, request: MarkupRequest):
        with app.state.db.connect() as conn:
            repo.get_chapter(conn, chapter_id)
        job = app.state.queue.enqueue(
            jobs.KIND_MARKUP, chapter_id,
            params={"model": request.model, "batch_chars": request.batch_chars,
                    "force": request.force},
        )
        return job.to_dict()

    def _job_view(conn, job) -> dict:
        """Задача с книгой, к которой она относится: по ней интерфейс ведёт на нужный экран."""
        data = job.to_dict()
        book_id = None
        if job.kind in ("book", "markup_book", "export"):
            book_id = job.target_id
        elif job.kind in ("synthesis", "markup") and job.target_id is not None:
            try:
                book_id = repo.get_chapter(conn, job.target_id).book_id
            except repo.RepoError:
                book_id = None  # главу уже удалили
        data["book_id"] = book_id
        return data

    @app.get("/api/jobs")
    def list_jobs(active: bool = False):
        with app.state.db.connect() as conn:
            items = [_job_view(conn, job) for job in repo.list_jobs(conn, active_only=active)]
            return {
                "jobs": items,
                "active": sum(1 for j in items if j["status"] in ("pending", "running", "cancelling")),
                # О завершённых интерфейс сообщит уведомлением.
                "announce": [_job_view(conn, job) for job in repo.unnotified_jobs(conn)],
            }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        """Одна задача — чтобы экран следил за своей, не читая всю очередь."""
        with app.state.db.connect() as conn:
            return _job_view(conn, repo.get_job(conn, job_id))

    @app.post("/api/books/{book_id}/markup")
    def markup_book(book_id: int, request: MarkupRequest):
        """Разметить книгу целиком: все главы без разметки, по очереди."""
        with app.state.db.connect() as conn:
            repo.get_book(conn, book_id)
        job = app.state.queue.enqueue(
            jobs.KIND_MARKUP_BOOK, book_id,
            params={"model": request.model, "batch_chars": request.batch_chars,
                    "force": request.force},
        )
        return job.to_dict()

    @app.get("/api/books/{book_id}/readiness")
    def book_readiness(book_id: int):
        with app.state.db.connect() as conn:
            repo.get_book(conn, book_id)
            return checks_mod.book_readiness(conn, book_id)

    @app.get("/api/continue")
    def continue_listening(limit: int = 6):
        """Что слушали последним — для главной страницы."""
        with app.state.db.connect() as conn:
            return {"items": repo.recent_playback(conn, limit=max(1, min(limit, 24)))}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        return app.state.queue.cancel(job_id).to_dict()

    @app.post("/api/jobs/{job_id}/announced")
    def announced(job_id: int):
        with app.state.db.connect() as conn:
            return repo.update_job_fields(conn, job_id, notified=True).to_dict()

    @app.delete("/api/jobs")
    def clear_jobs():
        with app.state.db.connect() as conn:
            return {"deleted": repo.clear_finished_jobs(conn)}

    @app.get("/api/chapters/{chapter_id}/audio")
    def chapter_audio(chapter_id: int):
        """Готовый файл главы. FileResponse отдаёт Range — плеер может перематывать."""
        with app.state.db.connect() as conn:
            chapter = repo.get_chapter(conn, chapter_id)
        if not chapter.audio_path:
            raise HTTPException(404, "глава ещё не озвучена")
        try:
            audio = paths.absolute(chapter.audio_path)
        except paths.PathsError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not audio.is_file():
            raise HTTPException(404, "файл озвучки не найден — озвучьте главу заново")
        return FileResponse(audio)

    # ---------------- воспроизведение ----------------

    def _chapter_entry(conn, chapter_id: int) -> dict:
        chapter = repo.get_chapter(conn, chapter_id)
        book = repo.get_book(conn, chapter.book_id)
        return {
            "chapter_id": chapter.id, "book_id": book.id, "book_title": book.title,
            "label": chapter.label, "number": chapter.number,
            "duration_ms": chapter.duration_ms, "ready": bool(chapter.audio_path),
        }

    @app.get("/api/books/{book_id}/playback")
    def get_playback(book_id: int):
        with app.state.db.connect() as conn:
            repo.get_book(conn, book_id)
            saved = repo.get_playback(conn, book_id)
            chapters = repo.list_chapters(conn, book_id)
            ready = [c for c in chapters if c.audio_path]
            return {
                "playback": saved,
                "chapters": [_chapter_entry(conn, c.id) for c in chapters],
                "ready": [c.id for c in ready],
            }

    @app.put("/api/books/{book_id}/playback")
    def set_playback(book_id: int, request: PlaybackRequest):
        with app.state.db.connect() as conn:
            repo.set_playback(conn, book_id, request.chapter_id, request.position_ms)
            return {"playback": repo.get_playback(conn, book_id)}

    @app.get("/api/queue")
    def get_queue():
        with app.state.db.connect() as conn:
            entries = []
            for chapter_id in repo.get_queue(conn):
                try:
                    entries.append(_chapter_entry(conn, chapter_id))
                except repo.RepoError:  # главу удалили, пока она стояла в очереди
                    continue
            return {"queue": entries}

    @app.put("/api/queue")
    def put_queue(request: QueueRequest):
        with app.state.db.connect() as conn:
            repo.set_queue(conn, request.chapter_ids)
            return {"queue": [_chapter_entry(conn, c) for c in repo.get_queue(conn)]}

    # ---------------- экспорт ----------------

    @app.post("/api/books/{book_id}/export")
    def export_book(book_id: int, request: ExportRequest):
        with app.state.db.connect() as conn:
            book = repo.get_book(conn, book_id)
        job = app.state.queue.enqueue(
            jobs.KIND_EXPORT, book_id,
            title=f"{jobs.EXPORT_TITLES.get(request.kind, 'Экспорт')}: {book.title}",
            params={"kind": request.kind},
        )
        return job.to_dict()

    @app.get("/api/books/{book_id}/exports")
    def book_exports(book_id: int):
        """Что уже выгружено для этой книги."""
        with app.state.db.connect() as conn:
            book = repo.get_book(conn, book_id)
        directory = export_mod.book_export_dir(book.id, book.title)
        files = [
            {
                "name": item.name, "size": item.stat().st_size,
                "url": f"/exports/{directory.name}/{item.name}",
            }
            for item in sorted(directory.glob("*")) if item.is_file()
        ]
        return {"dir": str(directory), "files": files}

    @app.post("/api/books/{book_id}/import-markup")
    async def import_markup(book_id: int, request: Request):
        """Разметка из JSON: тело запроса — сам файл."""
        from starlette.concurrency import run_in_threadpool

        try:
            payload = json.loads(await request.body())
        except ValueError as exc:
            raise HTTPException(400, "файл не разобрать как JSON") from exc

        def apply() -> dict:
            with app.state.db.connect() as conn:
                return export_mod.import_markup(conn, book_id, payload)

        return await run_in_threadpool(apply)

    # ---------------- статистика ----------------

    @app.get("/api/stats")
    def stats(days: int = 0):
        """Часы готового аудио и расход платных API.

        ``days`` — за сколько последних дней считать расход; 0 — за всё время.
        """
        since = None
        if days > 0:
            from datetime import datetime, timedelta, timezone

            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with app.state.db.connect() as conn:
            return {
                "audio": repo.audio_totals(conn),
                "books": repo.audio_by_book(conn),
                "usage": repo.usage_summary(conn, since),
                "usage_by_book": repo.usage_by_book(conn, since),
                "days": days,
            }

    # ---------------- словарь произношений ----------------

    @app.get("/api/books/{book_id}/pronunciations")
    def pronunciations(book_id: int):
        with app.state.db.connect() as conn:
            return {"rules": repo.list_pronunciations(conn, book_id)}

    @app.put("/api/books/{book_id}/pronunciations")
    def set_pronunciation(book_id: int, request: PronunciationRequest):
        with app.state.db.connect() as conn:
            repo.set_pronunciation(
                conn, book_id, request.term, request.replacement,
                whole_word=request.whole_word, case_sensitive=request.case_sensitive,
            )
            return {"rules": repo.list_pronunciations(conn, book_id)}

    @app.delete("/api/books/{book_id}/pronunciations/{rule_id}")
    def delete_pronunciation(book_id: int, rule_id: int):
        with app.state.db.connect() as conn:
            repo.delete_pronunciation(conn, rule_id)
            return {"rules": repo.list_pronunciations(conn, book_id)}

    @app.get("/api/pronunciations")
    def all_pronunciations():
        """Весь словарь: общие правила и правила каждой книги."""
        with app.state.db.connect() as conn:
            return {"rules": repo.all_pronunciations(conn)}

    @app.put("/api/pronunciations")
    def put_pronunciation(request: ScopedPronunciationRequest):
        with app.state.db.connect() as conn:
            repo.set_pronunciation(
                conn, request.book_id, request.term, request.replacement,
                whole_word=request.whole_word, case_sensitive=request.case_sensitive,
            )
            return {"rules": repo.all_pronunciations(conn)}

    @app.delete("/api/pronunciations/{rule_id}")
    def delete_any_pronunciation(rule_id: int):
        with app.state.db.connect() as conn:
            repo.delete_pronunciation(conn, rule_id)
            return {"rules": repo.all_pronunciations(conn)}

    # ---------------- студия ----------------

    @app.get("/api/studio")
    def studio():
        """Книги с их состоянием: что разобрано, озвучено, где нет голосов."""
        with app.state.db.connect() as conn:
            return {"books": checks_mod.studio_overview(conn)}

    # Статика — последней: маршруты API объявлены выше и не перекрываются.
    from fastapi.staticfiles import StaticFiles

    paths.ensure_layout()
    app.mount("/previews", StaticFiles(directory=paths.previews_dir()), name="previews")
    app.mount("/exports", StaticFiles(directory=paths.exports_dir()), name="exports")
    app.mount("/ui", StaticFiles(directory=WEB_DIR), name="ui")
    return app


def ready_line(port: int) -> str:
    """Строка, по которой десктоп понимает, что сервер слушает порт."""
    return READY_PREFIX + json.dumps({"port": port, "library": str(paths.library_root())})


ERROR_BROKEN_PIPE = 109  # Windows: другой конец канала закрыт
STDIN_POLL_SECONDS = 0.5


def _exit_when_stdin_closes(server) -> None:  # pragma: no cover — поток ОС
    """Родитель закрыл stdin — значит, окна больше нет.

    Надёжнее, чем ждать сигнала: срабатывает и когда оболочку убили, и когда
    она упала, а сиротой бэкенд держал бы базу и порт.

    На Windows stdin не читаем, а раз в полсекунды заглядываем в канал. Пока
    одна нить висит в ReadFile на канале, GetFileType на том же канале из
    другой нити тоже ждёт — а его вызывает библиотека C при загрузке DLL
    (OpenBLAS внутри numpy, который тянет torch). Блокирующее чтение здесь
    вешало весь бэкенд на первой же озвучке: окно оставалось пустым.
    """
    if sys.platform == "win32":
        _watch_stdin_pipe(server)
        return
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass
    server.should_exit = True


def _watch_stdin_pipe(server) -> None:  # pragma: no cover — только Windows
    import ctypes
    import msvcrt
    import time
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    try:
        fd = sys.stdin.fileno()
        handle = msvcrt.get_osfhandle(fd)
    except (OSError, ValueError, AttributeError):
        return
    available = wintypes.DWORD()
    while not server.should_exit:
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() == ERROR_BROKEN_PIPE:
                break
            return  # stdin — не канал (запуск из консоли): следить не за чем
        if available.value:
            # Нам в stdin ничего не пишут, но если напишут — не даём копиться.
            # Данные уже есть, поэтому чтение не повиснет.
            os.read(fd, available.value)
        time.sleep(STDIN_POLL_SECONDS)
    server.should_exit = True


# Десктоп просит «любой свободный порт», но память окна (localStorage) привязана
# к адресу вместе с портом: на новом порту каждый запуск забывал бы тему,
# скорость плеера и фильтры библиотеки. Поэтому сначала пробуем постоянный.
DESKTOP_PORT = 38417


def bind_socket(host: str, port: int, prefer: int | None = None) -> socket.socket:
    """Сокет на ``port``; при ``port=0`` — сперва на ``prefer``, если он свободен."""
    for candidate in ([prefer] if port == 0 and prefer else []) + [port]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, candidate))
            return sock
        except OSError:
            sock.close()
            if candidate == port:
                raise
    raise OSError(f"не удалось занять порт {port}")  # pragma: no cover


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    db: Database | None = None,
    base_url: str | None = None,
    token: str | None = None,
    announce: bool = False,
    exit_with_stdin: bool = False,
) -> None:  # pragma: no cover — блокирующий сервер
    """Запустить сервер.

    ``port=0`` — взять свободный порт. ``announce`` печатает строку готовности
    для десктопа уже после миграций и после ``listen``: подключение, пришедшее
    сразу за ней, ляжет в очередь сокета, а не получит отказ.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "нужен uvicorn: pip install -r requirements.txt"
        ) from exc

    app = create_app(db=db, base_url=base_url, token=token)
    # Незавершённые задачи возвращаются в очередь и продолжаются сами.
    app.state.queue.start()
    sock = bind_socket(host, port, prefer=DESKTOP_PORT if announce else None)
    sock.listen(128)
    actual_port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    if exit_with_stdin and sys.stdin is not None:
        threading.Thread(
            target=_exit_when_stdin_closes, args=(server,), daemon=True, name="stdin-watch"
        ).start()

    if announce:
        print(ready_line(actual_port), flush=True)
    else:
        print(f"BookTTS: http://{host}:{actual_port}")
        print(f"Библиотека: {paths.library_root()}")
    try:
        server.run(sockets=[sock])
    finally:
        app.state.queue.stop()
        # Рабочий Qwen держит модель в видеопамяти — отпускаем её сразу, а не
        # когда он сам заметит закрытый stdin.
        from ..core.engines import qwen as qwen_engine

        qwen_engine.shutdown()
