"""Схема SQLite и подключение к ней.

Всё состояние проекта живёт здесь: в памяти процесса ничего не кешируется,
поэтому веб-сервер, CLI и десктопная оболочка видят одно и то же.

``order`` и ``cast`` — зарезервированные слова SQL, поэтому в запросах они
всегда в двойных кавычках. Имена оставлены как в спецификации модели данных.

Схема версионируется через ``PRAGMA user_version``. Базовая схема — версия 1;
каждая следующая версия — миграция, которая применяется к существующей
библиотеке пользователя одной транзакцией: либо целиком, либо никак.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .. import paths

__all__ = ["Database", "SCHEMA_VERSION", "connect", "init_db", "fts_normalize"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS folder (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS book (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER REFERENCES folder(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',   -- относительный путь от корня библиотеки
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chapter (
    id          INTEGER PRIMARY KEY,
    book_id     INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (book_id, number)
);

CREATE TABLE IF NOT EXISTS segment (
    id          INTEGER PRIMARY KEY,
    chapter_id  INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
    "order"     INTEGER NOT NULL,
    speaker     TEXT NOT NULL DEFAULT 'narrator',
    text        TEXT NOT NULL,
    emotion     TEXT NOT NULL DEFAULT 'нейтрально',
    audio_path  TEXT,                        -- относительный путь, NULL пока не озвучено
    audio_hash  TEXT,                        -- hash(text + voice) на момент синтеза
    is_manual   INTEGER NOT NULL DEFAULT 0,  -- правил человек: авторазметка не трогает
    char_start  INTEGER,                     -- позиция в chapter.text, для подсветки
    char_end    INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS segment_chapter ON segment(chapter_id, "order");

CREATE TABLE IF NOT EXISTS voice (
    id           INTEGER PRIMARY KEY,
    engine       TEXT NOT NULL,
    voice_key    TEXT NOT NULL,              -- идентификатор внутри движка
    display_name TEXT NOT NULL DEFAULT '',
    gender       TEXT NOT NULL DEFAULT '',
    language     TEXT NOT NULL DEFAULT '',
    preview_path TEXT,
    UNIQUE (engine, voice_key)
);

CREATE TABLE IF NOT EXISTS "cast" (
    id        INTEGER PRIMARY KEY,
    book_id   INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    speaker   TEXT NOT NULL,
    voice_id  INTEGER REFERENCES voice(id) ON DELETE SET NULL,
    rate      REAL NOT NULL DEFAULT 1.0,
    pitch     REAL NOT NULL DEFAULT 1.0,
    volume    REAL NOT NULL DEFAULT 1.0,
    UNIQUE (book_id, speaker)
);

CREATE TABLE IF NOT EXISTS job (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                -- markup | synthesis | assemble
    target_id  INTEGER,
    status     TEXT NOT NULL DEFAULT 'pending',
    progress   REAL NOT NULL DEFAULT 0.0,
    total      INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS job_status ON job(status, kind);

-- История правок для undo/redo. Тоже в базе: глобальных переменных нет,
-- поэтому отмена переживает перезагрузку страницы.
CREATE TABLE IF NOT EXISTS edit_op (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    chapter_id  INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL,
    after_json  TEXT NOT NULL,
    undone      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS edit_op_scope ON edit_op(session_id, chapter_id, id);
"""


def fts_normalize(sql_expr: str) -> str:
    """SQL-выражение, приводящее текст к виду поискового индекса.

    ``unicode61`` сам снимает регистр, но «ё» и «е» для него разные буквы:
    без замены «ежик» не находил бы «Ёжик». Тот же приём — в запросе.
    """
    return f"replace(replace({sql_expr}, 'ё', 'е'), 'Ё', 'Е')"


# rowid в индексе кодирует и сущность, и её id: удаление — по первичному ключу,
# без полного прохода по таблице.
_FTS_KIND = {"folder": 1, "book": 2, "chapter": 3}


def _fts_triggers() -> str:
    folder_row = (
        "INSERT INTO search_index (rowid, kind, ref_id, title, body) "
        f"VALUES (new.id * 4 + 1, 'folder', new.id, {fts_normalize('new.name')}, '');"
    )
    book_row = (
        "INSERT INTO search_index (rowid, kind, ref_id, title, body) "
        f"VALUES (new.id * 4 + 2, 'book', new.id, {fts_normalize('new.title')}, "
        f"{fts_normalize('new.author')});"
    )
    chapter_row = (
        "INSERT INTO search_index (rowid, kind, ref_id, title, body) "
        f"VALUES (new.id * 4 + 3, 'chapter', new.id, {fts_normalize('new.title')}, "
        f"{fts_normalize('new.text')});"
    )
    return f"""
CREATE TRIGGER folder_fts_ai AFTER INSERT ON folder BEGIN {folder_row} END;
CREATE TRIGGER folder_fts_au AFTER UPDATE OF name ON folder BEGIN
    DELETE FROM search_index WHERE rowid = old.id * 4 + 1; {folder_row} END;
CREATE TRIGGER folder_fts_ad AFTER DELETE ON folder BEGIN
    DELETE FROM search_index WHERE rowid = old.id * 4 + 1; END;

CREATE TRIGGER book_fts_ai AFTER INSERT ON book BEGIN {book_row} END;
CREATE TRIGGER book_fts_au AFTER UPDATE OF title, author ON book BEGIN
    DELETE FROM search_index WHERE rowid = old.id * 4 + 2; {book_row} END;
CREATE TRIGGER book_fts_ad AFTER DELETE ON book BEGIN
    DELETE FROM search_index WHERE rowid = old.id * 4 + 2; END;

CREATE TRIGGER chapter_fts_ai AFTER INSERT ON chapter BEGIN {chapter_row} END;
CREATE TRIGGER chapter_fts_au AFTER UPDATE OF title, text ON chapter BEGIN
    DELETE FROM search_index WHERE rowid = old.id * 4 + 3; {chapter_row} END;
CREATE TRIGGER chapter_fts_ad AFTER DELETE ON chapter BEGIN
    DELETE FROM search_index WHERE rowid = old.id * 4 + 3; END;
"""


# Версия 2: десктоп — библиотека, плеер, фоновые задачи, экспорт.
MIGRATION_2 = f"""
ALTER TABLE folder  ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE book    ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE book    ADD COLUMN cover_path TEXT;                  -- относительный путь
ALTER TABLE book    ADD COLUMN language TEXT NOT NULL DEFAULT 'ru';

-- Склеенный файл главы и его отпечаток: по нему видно, что склейка устарела.
ALTER TABLE chapter ADD COLUMN audio_path TEXT;
ALTER TABLE chapter ADD COLUMN audio_hash TEXT;
ALTER TABLE chapter ADD COLUMN duration_ms INTEGER;

-- Где реплика звучит в файле главы: подсветка и перемотка в плеере.
ALTER TABLE segment ADD COLUMN audio_start_ms INTEGER;
ALTER TABLE segment ADD COLUMN audio_end_ms INTEGER;

ALTER TABLE job ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE job ADD COLUMN payload TEXT NOT NULL DEFAULT '{{}}';
ALTER TABLE job ADD COLUMN parent_id INTEGER REFERENCES job(id) ON DELETE CASCADE;
ALTER TABLE job ADD COLUMN started_at TEXT;
ALTER TABLE job ADD COLUMN finished_at TEXT;
ALTER TABLE job ADD COLUMN notified INTEGER NOT NULL DEFAULT 0;
CREATE INDEX job_parent ON job(parent_id);

-- Настройки приложения. Не секреты: ключи API — только в системном keyring.
CREATE TABLE setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Где остановились в каждой книге.
CREATE TABLE playback (
    book_id     INTEGER PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    chapter_id  INTEGER REFERENCES chapter(id) ON DELETE SET NULL,
    position_ms INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Очередь воспроизведения: видна и редактируется в плеере.
CREATE TABLE play_queue (
    id         INTEGER PRIMARY KEY,
    position   INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL REFERENCES chapter(id) ON DELETE CASCADE
);
CREATE INDEX play_queue_position ON play_queue(position);

-- Профиль озвучки: «персонаж -> голос», переносимый между книгами цикла.
-- Голос хранится по движку и ключу, а не по id: каталог голосов обновляется.
CREATE TABLE voice_profile (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE voice_profile_entry (
    id         INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES voice_profile(id) ON DELETE CASCADE,
    speaker    TEXT NOT NULL,
    engine     TEXT NOT NULL,
    voice_key  TEXT NOT NULL,
    rate       REAL NOT NULL DEFAULT 1.0,
    pitch      REAL NOT NULL DEFAULT 1.0,
    volume     REAL NOT NULL DEFAULT 1.0,
    UNIQUE (profile_id, speaker)
);

-- Словарь произношений книги: что TTS читает неправильно.
CREATE TABLE pronunciation (
    id             INTEGER PRIMARY KEY,
    book_id        INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    term           TEXT NOT NULL,
    replacement    TEXT NOT NULL,
    whole_word     INTEGER NOT NULL DEFAULT 1,
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    UNIQUE (book_id, term)
);

-- Расход платных API: символы и токены по каждому вызову.
CREATE TABLE api_usage (
    id            INTEGER PRIMARY KEY,
    service       TEXT NOT NULL,              -- anthropic | elevenlabs | silero | ...
    operation     TEXT NOT NULL DEFAULT '',   -- markup | synthesis | preview
    chars         INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    book_id       INTEGER REFERENCES book(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX api_usage_service ON api_usage(service, created_at);

-- Поиск по названиям и тексту глав.
CREATE VIRTUAL TABLE search_index USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);
{_fts_triggers()}
INSERT INTO search_index (rowid, kind, ref_id, title, body)
    SELECT id * 4 + 1, 'folder', id, {fts_normalize('name')}, '' FROM folder;
INSERT INTO search_index (rowid, kind, ref_id, title, body)
    SELECT id * 4 + 2, 'book', id, {fts_normalize('title')}, {fts_normalize('author')} FROM book;
INSERT INTO search_index (rowid, kind, ref_id, title, body)
    SELECT id * 4 + 3, 'chapter', id, {fts_normalize('title')}, {fts_normalize('text')} FROM chapter;
"""

# Версия 3: каталог голосов живёт в базе и обновляется у самих движков.
MIGRATION_3 = """
-- Голос пропал из каталога движка (удалён у ElevenLabs, выключен сервер Qwen).
-- Не удаляем строку: на неё может ссылаться уже назначенная роль.
ALTER TABLE voice ADD COLUMN available INTEGER NOT NULL DEFAULT 1;
ALTER TABLE voice ADD COLUMN tags TEXT NOT NULL DEFAULT '';
ALTER TABLE voice ADD COLUMN updated_at TEXT;
"""

# Версия 4: правило произношения может действовать во всех книгах сразу
# (book_id IS NULL) — «т. е.» → «то есть» незачем повторять в каждой книге.
# SQLite не умеет снять NOT NULL — таблицу пересобираем.
MIGRATION_4 = """
CREATE TABLE pronunciation_new (
    id             INTEGER PRIMARY KEY,
    book_id        INTEGER REFERENCES book(id) ON DELETE CASCADE,  -- NULL: для всех книг
    term           TEXT NOT NULL,
    replacement    TEXT NOT NULL,
    whole_word     INTEGER NOT NULL DEFAULT 1,
    case_sensitive INTEGER NOT NULL DEFAULT 0
);
INSERT INTO pronunciation_new (id, book_id, term, replacement, whole_word, case_sensitive)
    SELECT id, book_id, term, replacement, whole_word, case_sensitive FROM pronunciation;
DROP TABLE pronunciation;
ALTER TABLE pronunciation_new RENAME TO pronunciation;
-- NULL в UNIQUE друг с другом не совпадают, поэтому уникальность — по выражению.
CREATE UNIQUE INDEX pronunciation_scope_term ON pronunciation (COALESCE(book_id, 0), term);
"""

# Версия 5: сведения о книге — год, страна, описание, жанры, настроение и
# свои метки; отметка «глава прослушана». Списки хранятся JSON-массивами: фильтруются они в интерфейсе,
# а по жанру и настроению рисуется обложка.
MIGRATION_5 = """
ALTER TABLE book ADD COLUMN year INTEGER;
ALTER TABLE book ADD COLUMN country TEXT NOT NULL DEFAULT '';
ALTER TABLE book ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE book ADD COLUMN genres TEXT NOT NULL DEFAULT '[]';
ALTER TABLE book ADD COLUMN moods TEXT NOT NULL DEFAULT '[]';
ALTER TABLE book ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';

-- Прослушанная глава помечается и остаётся прослушанной, даже если книгу
-- начали слушать заново. Раньше это выводилось из текущей главы, поэтому
-- главы до неё считаем уже прослушанными.
ALTER TABLE chapter ADD COLUMN listened_at TEXT;
UPDATE chapter SET listened_at = datetime('now')
WHERE audio_path IS NOT NULL AND EXISTS (
    SELECT 1 FROM playback p JOIN chapter current ON current.id = p.chapter_id
    WHERE p.book_id = chapter.book_id AND chapter.number < current.number
);
-- Текущая глава, остановленная у самого конца, тоже дослушана.
UPDATE chapter SET listened_at = datetime('now')
WHERE listened_at IS NULL AND duration_ms IS NOT NULL AND EXISTS (
    SELECT 1 FROM playback p
    WHERE p.chapter_id = chapter.id
      AND p.position_ms >= MAX(chapter.duration_ms - 10000, chapter.duration_ms * 0.9)
);
"""

# Версия 6: откуда сведения о книге. «ai» — их подобрала нейросеть: книга
# помечается, и подбор не предлагается снова, как будто его не было.
MIGRATION_6 = """
ALTER TABLE book ADD COLUMN info_source TEXT NOT NULL DEFAULT '';
ALTER TABLE book ADD COLUMN info_at TEXT;
"""

# Версия 7: цвет роли закреплён за ней. Раньше цвет выдавался по частоте
# реплик и менялся, стоило поправить разметку; теперь он остаётся прежним,
# а человек может выбрать свой.
MIGRATION_7 = """
CREATE TABLE role_color (
    book_id  INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    speaker  TEXT NOT NULL,
    slot     INTEGER NOT NULL,
    PRIMARY KEY (book_id, speaker)
);
"""

MIGRATIONS: dict[int, str] = {
    2: MIGRATION_2, 3: MIGRATION_3, 4: MIGRATION_4, 5: MIGRATION_5, 6: MIGRATION_6,
    7: MIGRATION_7,
}
SCHEMA_VERSION = max(MIGRATIONS)


def init_db(conn: sqlite3.Connection) -> None:
    """Создать схему или довести существующую до последней версии."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA + "PRAGMA user_version = 1;")
        version = 1
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"библиотека создана более новой версией приложения "
            f"(схема {version}, поддерживается до {SCHEMA_VERSION})"
        )
    for target in range(version + 1, SCHEMA_VERSION + 1):
        # executescript сам коммитит перед запуском; явный BEGIN делает
        # миграцию атомарной — ALTER TABLE в SQLite транзакционен.
        try:
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;"
            )
        except Exception:
            conn.rollback()
            raise
    conn.commit()


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Бэкенд и фоновые задачи пишут из разных потоков: ждём блокировку,
    # а не падаем сразу с «database is locked».
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def connect(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Разовое подключение. Коммит на выходе, откат при исключении."""
    target = Path(path) if path else paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = _configure(sqlite3.connect(target))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class Database:
    """Хранит только путь к файлу — соединение открывается на операцию.

    Так безопасно ходить из разных потоков: sqlite3-соединение к потоку
    привязано, а общего соединения в процессе мы не держим.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else paths.db_path()

    def setup(self) -> "Database":
        with self.connect() as conn:
            init_db(conn)
        return self

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with connect(self.path) as conn:
            yield conn

    def __repr__(self) -> str:  # pragma: no cover
        return f"Database({self.path})"
