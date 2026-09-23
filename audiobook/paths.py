"""Единственное место, где живут пути.

Когда приложение переедет в Tauri или Electron, поменяется только
:func:`library_root` — всё остальное ходит через этот модуль.

Правило: в базе данных и в коде хранятся **относительные** пути от корня
библиотеки. Абсолютный путь появляется только в момент обращения к файлу,
через :func:`absolute`. Иначе библиотеку нельзя было бы перенести на другую
машину или в другую папку.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

__all__ = [
    "library_root",
    "set_library_root",
    "absolute",
    "relative",
    "db_path",
    "books_dir",
    "audio_dir",
    "cache_dir",
    "previews_dir",
    "exports_dir",
    "ensure_layout",
    "PathsError",
]

ENV_VAR = "BOOKTTS_HOME"
APP_DIR_NAME = "BookTTS"

_root: Path | None = None


class PathsError(RuntimeError):
    """Путь ведёт за пределы библиотеки или не может быть разрешён."""


def _platform_app_dir() -> Path:
    """Системная папка приложения — то, чем пользуется десктопная сборка."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_DIR_NAME


def library_root() -> Path:
    """Корень библиотеки.

    Порядок: явно заданный через :func:`set_library_root` -> переменная
    окружения ``BOOKTTS_HOME`` -> ``./library`` рядом с проектом, если она
    уже есть (режим разработки) -> системная папка приложения.

    Результат всегда разрешён (``resolve``). Это не косметика: Python из
    Microsoft Store подменяет ``%APPDATA%`` junction-ом в ``LocalCache``, и
    неразрешённый корень перестаёт быть префиксом разрешённого пути к файлу.
    """
    if _root is not None:
        return _root
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return Path(from_env).expanduser().resolve()
    local = Path.cwd() / "library"
    if local.is_dir():
        return local.resolve()
    return _platform_app_dir().resolve()


def set_library_root(path: str | Path | None) -> Path:
    """Переключить корень библиотеки (тесты, ``--library``, десктоп)."""
    global _root
    _root = Path(path).expanduser().resolve() if path is not None else None
    return library_root()


# --------------------------------------------------------------------------
# Раскладка внутри библиотеки
# --------------------------------------------------------------------------


def db_path() -> Path:
    return library_root() / "library.db"


def books_dir() -> Path:
    """Копии исходников книг."""
    return library_root() / "books"


def audio_dir() -> Path:
    """Готовые главы."""
    return library_root() / "audio"


def cache_dir() -> Path:
    """Озвученные сегменты."""
    return library_root() / "cache"


def previews_dir() -> Path:
    """Сэмплы голосов для предпрослушивания."""
    return library_root() / "previews"


def exports_dir() -> Path:
    """Готовые к выдаче наружу файлы: m4b, mp3, разметка."""
    return library_root() / "exports"


# --------------------------------------------------------------------------
# Библиотека, созданная Python из Microsoft Store
# --------------------------------------------------------------------------


def legacy_library_roots() -> list[Path]:
    """Библиотеки в песочнице Store-Python.

    Python из Microsoft Store подменяет ``%APPDATA%`` папкой внутри своего
    пакета. Собранное приложение этой подмены не видит и смотрит в настоящий
    ``%APPDATA%`` — библиотека, заведённая раньше, осталась бы невидимой.
    """
    if sys.platform != "win32":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    packages = Path(local) / "Packages"
    pattern = f"PythonSoftwareFoundation.Python.*/LocalCache/Roaming/{APP_DIR_NAME}"
    return sorted(p for p in packages.glob(pattern) if (p / "library.db").is_file())


def adopt_legacy_library() -> Path | None:
    """При первом запуске забрать старую библиотеку. Возвращает, откуда взята.

    Копируем, а не переносим: старая остаётся на месте, пока пользователь сам
    не решит её удалить. Ничего не делаем, если корень задан явно или в новом
    месте библиотека уже есть — чужие данные не перетираются никогда.
    """
    import shutil

    if _root is not None or os.environ.get(ENV_VAR):
        return None
    target = library_root()
    if (target / "library.db").exists():
        return None
    candidates = [p for p in legacy_library_roots() if p.resolve() != target]
    if not candidates:
        return None
    source = max(candidates, key=lambda p: (p / "library.db").stat().st_mtime)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    return source


def ensure_layout() -> Path:
    root = library_root()
    for directory in (root, books_dir(), audio_dir(), cache_dir(), previews_dir(), exports_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# Относительные <-> абсолютные
# --------------------------------------------------------------------------


def relative(path: str | Path) -> str:
    """Путь внутри библиотеки в виде относительной строки с «/».

    Разделитель всегда прямой слэш: база данных должна одинаково читаться
    и в Windows, и в Linux.
    """
    candidate = Path(path)
    root = library_root()
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise PathsError(
                f"путь {path} вне библиотеки {root} — в базе такому не место"
            ) from exc
    return PurePosixPath(*candidate.parts).as_posix()


def absolute(relative_path: str | Path) -> Path:
    """Абсолютный путь для относительного из базы, с проверкой выхода наружу."""
    candidate = PurePosixPath(str(relative_path).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PathsError(f"недопустимый относительный путь: {relative_path!r}")
    root = library_root()
    resolved = (root / Path(*candidate.parts)).resolve()
    # is_relative_to, а не startswith: иначе «library2» прошло бы как «library».
    if not resolved.is_relative_to(root):
        raise PathsError(f"путь {relative_path!r} выводит за пределы библиотеки")
    return resolved
