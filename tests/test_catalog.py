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
from audiobook.core.models import Voice
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
# Qwen3-TTS: рабочий процесс в своём окружении
# --------------------------------------------------------------------------

# Поддельный рабочий: тот же протокол JSON, что у настоящего, но без модели.
# Пишет настоящий wav и рядом — запрос, чтобы тест видел, что пришло.
FAKE_WORKER = r"""
import json, os, sys, wave
print("шум библиотек при загрузке", file=sys.stderr, flush=True)
if sys.argv[1] == "--fail":
    print(json.dumps({"ready": False, "error": "CUDA недоступна"}), flush=True)
    sys.exit(1)
print(json.dumps({"ready": True, "device": "cuda:0"}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    items = request.get("items") or [request]
    folder = os.path.dirname(items[0]["out"])
    with open(os.path.join(folder, "batches.log"), "a", encoding="utf-8") as log:
        log.write(f"{len(items)}\n")
    if any(item["text"] == "упади" for item in items):
        sys.exit(3)
    if any(item["text"] == "сбой" for item in items):
        print(json.dumps({"id": request["id"], "ok": False, "error": "сбой пачки"}), flush=True)
        continue
    for item in items:
        with wave.open(item["out"], "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
            handle.writeframes(b"\0\0" * 2400)
        with open(item["out"] + ".json", "w", encoding="utf-8") as handle:
            json.dump({**item, "hf_home": os.environ.get("HF_HOME", "")}, handle, ensure_ascii=False)
    print(json.dumps({"id": request["id"], "ok": True, "results": [{"ok": True}] * len(items)}), flush=True)
"""


@pytest.fixture()
def qwen_worker(tmp_path, library):
    import sys

    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    yield {"engine.qwen.python": sys.executable, "engine.qwen.worker": str(script)}
    qwen_module.shutdown()


def _last_request():
    # Имена файлов случайные — «последний» определяем по времени записи.
    requests = sorted((paths.cache_dir() / "qwen").glob("*.wav.json"),
                      key=lambda p: p.stat().st_mtime_ns)
    import json

    return json.loads(requests[-1].read_text(encoding="utf-8")) if requests else None


def test_qwen_without_its_environment_says_how_to_install(tmp_path):
    engine = qwen_module.engine({"engine.qwen.python": str(tmp_path / "нет" / "python.exe")})
    status = engine.status()
    assert status["ready"] is False and status["installed"] is False
    with pytest.raises(EngineError, match="не установлен"):
        engine.list_voices()
    with pytest.raises(EngineFatalError, match="не установлен"):
        engine.synthesize("Привет", "Ryan")


def test_qwen_voices_all_speak_russian(qwen_worker):
    voices = qwen_module.engine(qwen_worker).list_voices()
    assert {"Ryan", "Serena", "Uncle_Fu", "Vivian"} <= {v.key for v in voices}
    assert all(v.language == "ru" for v in voices)
    assert {v.gender for v in voices} == {"м", "ж"}


def test_qwen_synthesis_goes_through_the_worker(qwen_worker):
    data, extension = qwen_module.engine(qwen_worker).synthesize("Привет", "Ryan")
    assert extension == "wav" and data[:4] == b"RIFF"
    request = _last_request()
    assert request["language"] == "Russian" and request["speaker"] == "Ryan"
    # Временный файл за собой убран.
    assert not list((paths.cache_dir() / "qwen").glob("*.wav"))


def test_emotion_becomes_an_intonation_instruction(qwen_worker):
    qwen_module.engine(qwen_worker).synthesize("Уходи", "Ryan", emotion="зло")
    assert "зло" in _last_request()["instruct"].lower()
    qwen_module.engine(qwen_worker).synthesize("Привет", "Ryan", emotion="нейтрально")
    assert _last_request()["instruct"] == ""


def test_worker_is_started_once_and_reused(qwen_worker):
    engine = qwen_module.engine(qwen_worker)
    engine.synthesize("Раз", "Ryan")
    worker = engine._worker()
    pid = worker.process.pid
    engine.synthesize("Два", "Serena")
    assert worker.process.pid == pid
    assert "загружена" in engine.status()["detail"]


def test_model_that_failed_to_load_is_reported(qwen_worker):
    options = {**qwen_worker, "engine.qwen.model": "--fail"}
    with pytest.raises(EngineFatalError, match="CUDA недоступна"):
        qwen_module.engine(options).synthesize("Привет", "Ryan")


def test_crashed_worker_is_restarted_on_the_next_line(qwen_worker):
    engine = qwen_module.engine(qwen_worker)
    with pytest.raises(EngineError, match="завершился"):
        engine.synthesize("упади", "Ryan")
    data, _ = engine.synthesize("Живой", "Ryan")
    assert data[:4] == b"RIFF"


def test_unknown_qwen_voice_is_refused(qwen_worker):
    with pytest.raises(EngineFatalError, match="нет голоса"):
        qwen_module.engine(qwen_worker).synthesize("Привет", "Cherry")


def _qwen_chapter(conn, marked, qwen_worker, batch):
    """Все роли главы — на голосе Qwen, рабочий поддельный."""
    for key, value in {**qwen_worker, "engine.qwen.batch": batch}.items():
        repo.set_setting(conn, key, value)
    voice = repo.upsert_voice(conn, Voice(engine="qwen", voice_key="Ryan", display_name="Ryan"))
    for speaker in repo.book_speakers(conn, marked.book_id):
        repo.set_cast(conn, marked.book_id, speaker, voice.id)


def _batches() -> list[int]:
    log = paths.cache_dir() / "qwen" / "batches.log"
    return [int(n) for n in log.read_text(encoding="utf-8").split()] if log.exists() else []


def test_qwen_lines_are_voiced_in_batches(conn, marked, qwen_worker):
    """Пачка из восьми идёт в пять раз быстрее, чем восемь по одной."""
    from audiobook.core import synth

    _qwen_chapter(conn, marked, qwen_worker, batch=2)
    result = synth.synthesize_chapter(conn, marked.id)
    assert result["done"] == 5 and result["failed"] == []
    assert _batches() == [2, 2, 1]
    assert all(s.audio_path for s in repo.list_segments(conn, marked.id))


def test_failed_batch_is_retried_line_by_line(conn, marked, qwen_worker, monkeypatch):
    """Одна кривая реплика не должна губить остальные в своей пачке."""
    from audiobook.core import synth

    monkeypatch.setattr(synth, "BACKOFF", (0.0, 0.0))
    _qwen_chapter(conn, marked, qwen_worker, batch=2)
    bad = repo.list_segments(conn, marked.id)[1]
    repo.update_segment(conn, bad.id, text="сбой")

    result = synth.synthesize_chapter(conn, marked.id)
    assert [f["segment_id"] for f in result["failed"]] == [bad.id]
    voiced = [s for s in repo.list_segments(conn, marked.id) if s.audio_path]
    assert len(voiced) == 4


def test_qwen_home_holds_both_environment_and_model(qwen_worker, tmp_path):
    """Папку Qwen можно вынести на другой диск: окружение и модель живут вместе."""
    home = tmp_path / "E" / "llm" / "BookTTS-qwen"
    engine = qwen_module.engine({"engine.qwen.home": str(home)})
    assert engine.python == qwen_module.python_in(home)
    assert engine.status()["installed"] is False
    assert str(home) in engine.status()["detail"]

    (home / "huggingface").mkdir(parents=True)
    qwen_module.engine({**qwen_worker, "engine.qwen.home": str(home)}).synthesize("Привет", "Ryan")
    assert _last_request()["hf_home"] == str(home / "huggingface")


def test_without_its_own_model_folder_the_shared_cache_is_used(qwen_worker, tmp_path):
    """Иначе у тех, кто ничего не переносил, модель скачалась бы заново."""
    import os

    options = {**qwen_worker, "engine.qwen.home": str(tmp_path / "без-модели")}
    qwen_module.engine(options).synthesize("Привет", "Ryan")
    assert _last_request()["hf_home"] == os.environ.get("HF_HOME", "")


def test_only_qwen_hears_emotions():
    from audiobook.core.engines import uses_emotion

    assert uses_emotion("qwen") is True
    assert uses_emotion("silero") is False


def test_worker_file_is_not_mistaken_for_an_engine():
    """Рабочий скрипт подменяет stdout — импортировать его при поиске движков нельзя."""
    assert "_qwen_worker" not in engine_names()


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
