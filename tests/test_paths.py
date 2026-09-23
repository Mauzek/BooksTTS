"""Тесты модуля путей: в базу попадают только относительные пути."""

from __future__ import annotations

import pytest

from audiobook import paths


def test_layout_is_created(library):
    assert paths.books_dir().is_dir()
    assert paths.audio_dir().is_dir()
    assert paths.cache_dir().is_dir()
    assert paths.previews_dir().is_dir()


def test_absolute_path_becomes_relative(library):
    inside = paths.books_dir() / "книга.epub"
    assert paths.relative(inside) == "books/книга.epub"


def test_separator_is_always_forward_slash(library):
    assert "\\" not in paths.relative(paths.audio_dir() / "гл" / "001.mp3")


def test_relative_stays_relative(library):
    assert paths.relative("audio/001.mp3") == "audio/001.mp3"


def test_round_trip(library):
    original = paths.cache_dir() / "abc.wav"
    assert paths.absolute(paths.relative(original)) == original


def test_path_outside_library_is_refused(library, tmp_path):
    with pytest.raises(paths.PathsError, match="вне библиотеки"):
        paths.relative(tmp_path / "чужое" / "файл.mp3")


@pytest.mark.parametrize("bad", ["../secret", "audio/../../secret", "/etc/passwd"])
def test_escape_attempts_are_refused(library, bad):
    with pytest.raises(paths.PathsError):
        paths.absolute(bad)


def test_root_follows_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path / "своя"))
    paths.set_library_root(None)
    try:
        assert paths.library_root() == (tmp_path / "своя").resolve()
    finally:
        paths.set_library_root(None)


def test_explicit_root_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path / "из-окружения"))
    paths.set_library_root(tmp_path / "явная")
    try:
        assert paths.library_root() == (tmp_path / "явная").resolve()
    finally:
        paths.set_library_root(None)


def test_root_is_always_resolved(monkeypatch, tmp_path):
    """Python из Microsoft Store подменяет %APPDATA% junction-ом.

    Неразрешённый корень тогда перестаёт быть префиксом разрешённого пути
    к файлу, и импорт книги падал на ровном месте.
    """
    monkeypatch.delenv(paths.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    paths.set_library_root(None)
    try:
        root = paths.library_root()
        assert root == root.resolve()
    finally:
        paths.set_library_root(None)


def test_sibling_directory_is_not_inside(library):
    """«library2» рядом с «library» — не то же самое, что внутри неё."""
    with pytest.raises(paths.PathsError):
        paths.absolute("../library2/книга.txt")
