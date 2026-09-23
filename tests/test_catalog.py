"""Движки, каталог голосов, профили и хранение ключей.

Ни один тест не ходит в сеть, не грузит модели и не пишет в настоящее
системное хранилище паролей.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from audiobook import paths
from audiobook.core import catalog, repo, secrets
from audiobook.core.engines import (
    EngineError,
    EngineFatalError,
    VoiceInfo,
    engine_names,
    get_engine,
)
from audiobook.core.engines import elevenlabs as elevenlabs_module
from audiobook.core.engines import qwen as qwen_module
from audiobook.core.engines import silero as silero_module


# --------------------------------------------------------------------------
# Поддельный движок
# --------------------------------------------------------------------------


class FakeEngine:
    name = "silero"
    title = "Подделка"

    def __init__(self, voices=None, fail: str = "") -> None:
        self.voices = voices if voices is not None else [
            VoiceInfo("aidar", "aidar", "м", "ru", ["ровный"]),
            VoiceInfo("baya", "baya", "ж", "ru"),
            VoiceInfo("kseniya", "kseniya", "ж", "ru"),
        ]
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def list_voices(self):
        if self.fail:
            raise EngineError(self.fail)
        return self.voices

    def supports_language(self, language: str) -> bool:
        return language.startswith("ru")

    def status(self):
        return {"name": self.name, "title": self.title, "ready": not self.fail, "detail": ""}

    def synthesize(self, text, voice_key, *, rate=1.0, pitch=1.0, volume=1.0):
        if self.fail:
            raise EngineError(self.fail)
        self.calls.append((text, voice_key))
        return b"RIFF....WAVEfake", "wav"


@pytest.fixture()
def fake(monkeypatch):
    """Подменяет движок, который каталог берёт по имени."""
    engine = FakeEngine()
    monkeypatch.setattr(catalog, "get_engine", lambda name, options=None: engine)
    monkeypatch.setattr(catalog, "engine_names", lambda: ["silero"])
    return engine


# --------------------------------------------------------------------------
# Движки находятся сами
# --------------------------------------------------------------------------


def test_engines_are_discovered_by_file():
    assert engine_names() == ["elevenlabs", "qwen", "silero"]


def test_silero_lists_the_verified_speakers():
    engine = get_engine("silero")
    keys = {voice.key for voice in engine.list_voices()}
    assert keys == {"aidar", "baya", "kseniya", "eugene", "xenia"}
    # «random» даёт разный голос на каждый вызов — персонаж потерял бы единство.
    assert "random" not in keys
    assert engine.supports_language("ru") and not engine.supports_language("en")


def test_silero_model_version_comes_from_settings():
    engine = get_engine("silero", {"engine.silero.model": "v4_ru"})
    assert engine.status()["variant"] == "v4_ru"
    assert "v4_ru" in engine.list_voices()[0].tags


def test_device_without_cuda_is_explained_before_synthesis(monkeypatch):
    """Иначе «cuda» в настройках всплывает невнятной ошибкой на каждой реплике."""
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    problem = silero_module.device_problem("cuda")
    assert "cpu" in problem
    assert silero_module.device_problem("cpu") == ""

    engine = silero_module.engine({"engine.silero.device": "cuda"})
    assert engine.status()["ready"] is False
    with pytest.raises(EngineFatalError):
        engine.synthesize("Привет", "aidar")


def test_unusable_device_is_refused_on_save(conn, monkeypatch):
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(repo.InvalidOperation, match="cpu"):
        catalog.save_settings(conn, {"engine.silero.device": "cuda"})
    assert catalog.settings(conn)["engine.silero.device"] == "cpu"


def test_silero_splits_long_text_by_sentences():
    parts = silero_module.split_for_tts("Раз. " * 400)
    assert len(parts) > 1
    assert all(len(p) <= silero_module.MAX_CHUNK_CHARS for p in parts)


# --------------------------------------------------------------------------
# ElevenLabs: запросы подменены
# --------------------------------------------------------------------------


@dataclass
class FakeResponse:
    payload: Any = None
    status_code: int = 200
    content: bytes = b""
    text: str = ""

    def json(self):
        return self.payload


def elevenlabs_with(handler):
    return elevenlabs_module.engine({"engine.elevenlabs.client": handler})


def test_elevenlabs_walks_all_pages_of_the_catalog():
    seen = []

    def handler(method, path, **kwargs):
        seen.append((method, path, kwargs.get("params")))
        token = (kwargs.get("params") or {}).get("next_page_token")
        if not token:
            return FakeResponse({
                "voices": [{"voice_id": "v1", "name": "Рэйчел",
                            "labels": {"gender": "female", "accent": "american"}}],
                "next_page_token": "page2",
            })
        return FakeResponse({
            "voices": [{"voice_id": "v2", "name": "Джош", "labels": {"gender": "male"}}],
            "next_page_token": None,
        })

    voices = elevenlabs_with(handler).list_voices()
    assert [v.key for v in voices] == ["v1", "v2"]
    assert [v.gender for v in voices] == ["ж", "м"]
    assert seen[0][1] == "/v2/voices"


def test_elevenlabs_reports_remaining_quota(monkeypatch):
    monkeypatch.setattr(secrets, "get_key", lambda name: "ключ")
    handler = lambda *a, **k: FakeResponse(  # noqa: E731 — однострочная подмена
        {"subscription": {"tier": "starter", "character_count": 3000, "character_limit": 30000}}
    )
    status = elevenlabs_with(handler).status()
    assert status["ready"] is True
    assert "осталось 27000" in status["detail"]


def test_elevenlabs_without_key_is_not_ready(monkeypatch):
    monkeypatch.setattr(secrets, "get_key", lambda name: "")
    assert elevenlabs_module.engine({}).status() == {
        "name": "elevenlabs", "title": elevenlabs_module.TITLE, "needs_key": True,
        "variant": elevenlabs_module.DEFAULT_MODEL, "ready": False, "detail": "нет ключа",
    }


def test_elevenlabs_synthesis_asks_for_a_playable_format():
    captured = {}

    def handler(method, path, **kwargs):
        captured.update(path=path, params=kwargs.get("params"), json=kwargs.get("json"))
        return FakeResponse(content=b"ID3mp3")

    data, extension = elevenlabs_with(handler).synthesize("Привет", "v1", rate=1.5)
    assert (data, extension) == (b"ID3mp3", "mp3")
    assert captured["path"] == "/v1/text-to-speech/v1"
    assert captured["params"]["output_format"].startswith("mp3")
    # Скорость у ElevenLabs живёт в узком диапазоне — за него не выходим.
    assert captured["json"]["voice_settings"]["speed"] == 1.2


# --------------------------------------------------------------------------
# Qwen3-TTS: локальный Gradio
# --------------------------------------------------------------------------

QWEN_INFO = {
    "named_endpoints": {
        "/generate_speech": {
            "parameters": [
                {"label": "Text", "parameter_name": "text", "type": {"type": "string"}},
                {"label": "Voice", "parameter_name": "voice",
                 "type": {"type": "string", "enum": ["Cherry", "Ryan", "Сергей"]}},
            ]
        }
    }
}


def test_qwen_takes_voices_from_the_running_server():
    def transport(method, path, **kwargs):
        assert path in ("/gradio_api/info", "/info")
        return FakeResponse(QWEN_INFO)

    voices = qwen_module.engine({"engine.qwen.transport": transport}).list_voices()
    assert [v.key for v in voices] == ["Cherry", "Ryan", "Сергей"]


def test_qwen_without_a_server_refuses_instead_of_inventing_voices():
    """Иначе в каталог попали бы голоса, которыми нечем озвучить."""
    def transport(method, path, **kwargs):
        raise EngineError("сервер не отвечает")

    engine = qwen_module.engine({"engine.qwen.transport": transport})
    with pytest.raises(EngineError, match="не отвечает"):
        engine.list_voices()
    assert engine.status()["ready"] is False


def test_qwen_falls_back_to_built_in_voices_when_the_server_is_terse():
    """Сервер отвечает, но список голосов в описании не отдал."""
    def transport(method, path, **kwargs):
        return FakeResponse({"named_endpoints": {"/tts": {"parameters": [
            {"label": "Text", "parameter_name": "text", "type": {"type": "string"}},
        ]}}})

    voices = qwen_module.engine({"engine.qwen.transport": transport}).list_voices()
    assert {"Vivian", "Ryan", "Sohee"} <= {v.key for v in voices}
    assert all("встроенный" in v.tags for v in voices)


def test_qwen_supports_russian():
    assert qwen_module.engine({}).supports_language("ru")


def test_qwen_downloads_the_generated_file():
    def transport(method, path, **kwargs):
        if path.endswith("/info"):
            return FakeResponse(QWEN_INFO)
        if path == "/gradio_api/call/generate_speech":
            return FakeResponse({"event_id": "e1"})
        if path == "/gradio_api/call/generate_speech/e1":
            return FakeResponse(text='event: complete\ndata: [{"path": "/tmp/out.wav"}]\n')
        if path == "/gradio_api/file=/tmp/out.wav":
            return FakeResponse(content=b"RIFFwav")
        raise AssertionError(f"неожиданный путь {path}")

    engine = qwen_module.engine({"engine.qwen.transport": transport})
    assert engine.synthesize("Привет", "Cherry") == (b"RIFFwav", "wav")


# --------------------------------------------------------------------------
# Каталог
# --------------------------------------------------------------------------


def test_refresh_fills_the_catalog(conn, fake):
    report = catalog.refresh(conn)
    assert report[0]["added"] == 3
    assert {v.voice_key for v in repo.list_voices(conn)} == {"aidar", "baya", "kseniya"}


def test_disappeared_voice_is_marked_not_deleted(conn, fake, chapter):
    catalog.refresh(conn)
    voice = next(v for v in repo.list_voices(conn) if v.voice_key == "kseniya")
    repo.set_cast(conn, chapter.book_id, "Аглая", voice.id)

    fake.voices = fake.voices[:2]
    report = catalog.refresh(conn)
    assert report[0]["gone"] == 1
    # Роль по-прежнему ссылается на голос, но он помечен недоступным.
    assert repo.get_voice(conn, voice.id).available is False
    assert repo.get_cast(conn, chapter.book_id, "Аглая").voice_id == voice.id
    assert [v.voice_key for v in repo.list_voices(conn, available_only=True)] == ["aidar", "baya"]


def test_refresh_reports_an_unavailable_engine(conn, monkeypatch):
    broken = FakeEngine(fail="сервер не отвечает")
    monkeypatch.setattr(catalog, "get_engine", lambda name, options=None: broken)
    monkeypatch.setattr(catalog, "engine_names", lambda: ["qwen"])
    assert catalog.refresh(conn)[0]["error"] == "сервер не отвечает"


def test_preview_is_written_once_and_reused(conn, library, fake):
    catalog.refresh(conn)
    voice = repo.list_voices(conn)[0]

    first = catalog.preview(conn, voice.id)
    again = catalog.preview(conn, voice.id)
    assert first["url"] == again["url"]
    assert len(fake.calls) == 1  # второй раз взяли готовый файл
    assert paths.absolute(first["path"]).is_file()
    assert repo.get_voice(conn, voice.id).preview_path == first["path"]
    assert repo.usage_summary(conn)[0]["service"] == "silero"


def test_custom_phrase_does_not_replace_the_stored_preview(conn, library, fake):
    catalog.refresh(conn)
    voice = repo.list_voices(conn)[0]
    catalog.preview(conn, voice.id)
    stored = repo.get_voice(conn, voice.id).preview_path

    custom = catalog.preview(conn, voice.id, text="Своя фраза для проверки.")
    assert custom["path"] != stored
    assert repo.get_voice(conn, voice.id).preview_path == stored


def test_preview_of_a_broken_engine_is_a_clear_error(conn, library, fake):
    catalog.refresh(conn)
    voice = repo.list_voices(conn)[0]
    fake.fail = "модель не загрузилась"
    with pytest.raises(catalog.CatalogError, match="не загрузилась"):
        catalog.preview(conn, voice.id)


# --------------------------------------------------------------------------
# Назначение ролей
# --------------------------------------------------------------------------


def test_auto_assign_is_stable_and_spreads_voices(conn, fake, marked):
    catalog.refresh(conn)
    first = catalog.auto_assign(conn, marked.book_id)
    names = {item["speaker"]: item["voice"]["voice_key"] for item in first}

    assert names["narrator"] == "aidar"  # рассказчику — ровный мужской
    assert names["Велимир"] != names["Аглая"]

    # Повторный вызов ничего не меняет: назначенное не трогаем.
    assert catalog.auto_assign(conn, marked.book_id) == []
    # А при перезаписи выбор тот же самый.
    again = catalog.auto_assign(conn, marked.book_id, overwrite=True)
    assert {i["speaker"]: i["voice"]["voice_key"] for i in again} == names


def test_auto_assign_without_a_catalog_explains_what_to_do(conn, marked):
    with pytest.raises(catalog.CatalogError, match="обновите каталог"):
        catalog.auto_assign(conn, marked.book_id)


def test_assigning_a_missing_voice_is_refused(conn, marked):
    with pytest.raises(repo.RepoError):
        catalog.assign(conn, marked.book_id, "Аглая", 999)


# --------------------------------------------------------------------------
# Профили
# --------------------------------------------------------------------------


def test_profile_moves_cast_to_another_book_matching_similar_names(conn, fake, marked):
    catalog.refresh(conn)
    catalog.auto_assign(conn, marked.book_id)
    catalog.save_profile(conn, marked.book_id, "Цикл «Рынок»")

    second = repo.create_book(conn, "Вторая книга")
    chapter = repo.create_chapter(conn, second.id, 1, "", "Текст")
    repo.replace_segments(conn, chapter.id, [
        repo.Segment(speaker="narrator", text="Текст"),
        repo.Segment(speaker="Велемир", text="Реплика"),  # в первой книге «Велимир»
        repo.Segment(speaker="Терех", text="Другой"),
    ])

    profile = repo.list_profiles(conn)[0]
    result = catalog.apply_profile(conn, second.id, profile["id"])
    applied = {item["speaker"] for item in result["applied"]}
    assert applied == {"narrator", "Велемир"}
    assert "Терех" in result["without_voice"]
    assert "Аглая" in result["unmatched"]


def test_profile_reports_a_voice_that_left_the_catalog(conn, fake, marked):
    catalog.refresh(conn)
    catalog.auto_assign(conn, marked.book_id)
    catalog.save_profile(conn, marked.book_id, "Профиль")
    conn.execute("DELETE FROM voice")

    profile = repo.list_profiles(conn)[0]
    result = catalog.apply_profile(conn, marked.book_id, profile["id"])
    assert result["applied"] == []
    assert len(result["missing_voice"]) >= 1


def test_saving_a_profile_without_voices_is_refused(conn, marked):
    with pytest.raises(catalog.CatalogError, match="нет назначенных"):
        catalog.save_profile(conn, marked.book_id, "Пустой")


# --------------------------------------------------------------------------
# Ключи API
# --------------------------------------------------------------------------


class FakeKeyring:
    """Хранилище в памяти: настоящий keyring в тестах не трогаем."""

    store: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture()
def vault(monkeypatch):
    FakeKeyring.store = {}
    monkeypatch.setattr(secrets, "_keyring", FakeKeyring)
    return FakeKeyring.store


def test_key_is_stored_and_read_back(vault):
    secrets.set_key("anthropic", "sk-тест-1234567890")
    assert secrets.get_key("anthropic") == "sk-тест-1234567890"
    secrets.delete_key("anthropic")
    assert secrets.get_key("anthropic") == ""


def test_environment_key_is_used_but_marked_as_such(vault, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "из-окружения-987654")
    entry = next(e for e in secrets.status() if e["service"] == "elevenlabs")
    assert entry["source"] == "env" and entry["can_import_env"] is True

    assert secrets.import_from_env("elevenlabs") is True
    entry = next(e for e in secrets.status() if e["service"] == "elevenlabs")
    assert entry["source"] == "keyring" and entry["can_import_env"] is False


def test_status_never_reveals_the_key(vault):
    secrets.set_key("anthropic", "sk-очень-секретный-ключ")
    text = repr(secrets.status())
    assert "секретный" not in text
    assert "…ключ" in text


def test_empty_key_is_refused(vault):
    with pytest.raises(secrets.SecretsError):
        secrets.set_key("anthropic", "   ")


def test_unknown_service_is_named(vault):
    with pytest.raises(secrets.SecretsError, match="неизвестная служба"):
        secrets.get_key("openai")
