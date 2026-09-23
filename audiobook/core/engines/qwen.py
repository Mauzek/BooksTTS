"""Qwen3-TTS: локальный синтез на видеокарте.

Модель живёт в отдельном окружении Python (torch с CUDA, пакет qwen-tts) и
работает в дочернем процессе — рабочем. Приложение запускает его само при
первом синтезе, держит, пока открыто, и общается строками JSON через
stdin/stdout. Никаких портов и ручного запуска сервера.

Голоса — девять встроенных голосов модели CustomVoice. По карточке модели
(сверено 23.09.2026) все они говорят на всех десяти языках модели, включая
русский, хотя у каждого есть «родной». Эмоция реплики передаётся модели как
инструкция к интонации.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from . import EngineError, EngineFatalError, VoiceInfo

NAME = "qwen"
TITLE = "Qwen3-TTS (видеокарта)"

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
LANGUAGE = "Russian"
# Первый запуск скачивает модель (несколько гигабайт) — ждём долго.
READY_TIMEOUT = 1800.0
REQUEST_TIMEOUT = 600.0  # пачка из восьми длинных реплик на слабой карте
# Замерено на RTX 3070, модель 1.7B: пачка по 8 — 0,78× реального времени
# при 5,1 ГБ видеопамяти; по одной — 0,15×.
DEFAULT_BATCH = 8

# Встроенные голоса CustomVoice: ключ, пол, родной язык.
VOICES = [
    ("Ryan", "м", "en", "энергичный, ритмичный"),
    ("Aiden", "м", "en", "солнечный, американский"),
    ("Uncle_Fu", "м", "zh", "зрелый, низкий и мягкий"),
    ("Dylan", "м", "zh", "молодой, пекинский"),
    ("Eric", "м", "zh", "живой, чэндуский"),
    ("Vivian", "ж", "zh", "яркая, с характером"),
    ("Serena", "ж", "zh", "тёплая, мягкая"),
    ("Ono_Anna", "ж", "ja", "игривая"),
    ("Sohee", "ж", "ko", "тёплая"),
]

# Эмоция из разметки -> инструкция к интонации.
INSTRUCTIONS = {
    "радостно": "Говори радостно, с улыбкой в голосе",
    "зло": "Говори зло, раздражённо и резко",
    "грустно": "Говори грустно, тихо и устало",
}

# Подчёркивание в имени — чтобы поиск движков не импортировал рабочий скрипт.
WORKER = Path(__file__).with_name("_qwen_worker.py")
# Эмоция меняет звук: для этого движка она входит в отпечаток реплики.
USES_EMOTION = True


def default_home() -> Path:
    """Папка Qwen по умолчанию: вне проекта и вне OneDrive."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "BookTTS-qwen"


def python_in(home: Path) -> Path:
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    exe = "python.exe" if sys.platform == "win32" else "python"
    return home / "venv" / scripts / exe


def default_python() -> Path:
    return python_in(default_home())


class _Worker:
    """Дочерний процесс с загруженной моделью. Один на процесс приложения."""

    def __init__(self, python: Path, worker: Path, model: str,
                 hf_home: Path | None = None) -> None:
        self.python, self.worker, self.model = python, worker, model
        self.hf_home = hf_home
        self.process: subprocess.Popen | None = None
        self.device = ""
        self.lock = threading.Lock()
        self.errors: deque[str] = deque(maxlen=40)
        self.next_id = 0

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _drain_stderr(self, stream) -> None:
        for line in stream:
            self.errors.append(line.rstrip())

    def _read(self, timeout: float) -> dict[str, Any]:
        """Строка ответа от рабочего с ограничением по времени."""
        result: dict[str, Any] = {}

        def read() -> None:
            line = self.process.stdout.readline()
            if line:
                try:
                    result.update(json.loads(line))
                except ValueError:
                    result["error"] = f"непонятный ответ рабочего процесса: {line[:200]}"

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            self.stop()
            raise EngineError("Qwen3-TTS не ответил вовремя — процесс перезапустится")
        if not result:
            tail = "\n".join(list(self.errors)[-5:])
            self.stop()
            raise EngineError(f"процесс Qwen3-TTS завершился{': ' + tail if tail else ''}")
        return result

    def start(self) -> None:
        if self.alive():
            return
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        if self.hf_home is not None:
            # Модель лежит рядом с окружением — например, вынесена на другой диск.
            environment["HF_HOME"] = str(self.hf_home)
        try:
            self.process = subprocess.Popen(
                [str(self.python), str(self.worker), self.model],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1, creationflags=flags, env=environment,
            )
        except OSError as exc:
            raise EngineFatalError(f"не удалось запустить Qwen3-TTS: {exc}") from exc
        threading.Thread(target=self._drain_stderr, args=(self.process.stderr,), daemon=True).start()
        hello = self._read(READY_TIMEOUT)
        if not hello.get("ready"):
            self.stop()
            raise EngineFatalError(f"модель Qwen3-TTS не загрузилась: {hello.get('error', 'без причины')}")
        self.device = hello.get("device", "")

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.start()
            self.next_id += 1
            payload = {**payload, "id": self.next_id}
            try:
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except OSError as exc:
                self.stop()
                raise EngineError(f"связь с Qwen3-TTS потеряна: {exc}") from exc
            return self._read(REQUEST_TIMEOUT)

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.stdin.close()  # рабочий сам выходит, когда закрыт stdin
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.process.kill()
        self.process = None


# Модель занимает гигабайты видеопамяти — один рабочий на процесс приложения.
_WORKERS: dict[tuple[str, str, str, str], _Worker] = {}
_WORKERS_LOCK = threading.Lock()


class QwenEngine:
    name = NAME
    title = TITLE
    uses_emotion = True  # эмоция меняет звук — входит в отпечаток реплики

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        # Папка Qwen: окружение (venv) и, если есть, кеш модели (huggingface).
        # Целиком переносится на другой диск — достаточно поменять настройку.
        self.home = Path(options.get("engine.qwen.home") or default_home()).expanduser()
        self.python = Path(options.get("engine.qwen.python") or python_in(self.home))
        hf_home = self.home / "huggingface"
        # Своей папки с моделью нет — общий кеш Hugging Face, как раньше:
        # иначе у тех, кто ничего не переносил, модель скачалась бы заново.
        self.hf_home = hf_home if hf_home.is_dir() else None
        self.model = options.get("engine.qwen.model") or DEFAULT_MODEL
        self.worker_script = Path(options.get("engine.qwen.worker") or WORKER)
        try:
            self.batch_size = max(1, int(options.get("engine.qwen.batch") or DEFAULT_BATCH))
        except (TypeError, ValueError):
            self.batch_size = DEFAULT_BATCH

    # -- окружение -------------------------------------------------------

    def installed(self) -> bool:
        return self.python.is_file()

    def _worker(self) -> _Worker:
        if not self.installed():
            raise EngineFatalError(
                "Qwen3-TTS не установлен: нет окружения "
                f"{self.python} — установите его на экране «Голоса»"
            )
        key = self._key()
        with _WORKERS_LOCK:
            if key not in _WORKERS:
                _WORKERS[key] = _Worker(self.python, self.worker_script, self.model, self.hf_home)
            return _WORKERS[key]

    def _key(self) -> tuple[str, str, str, str]:
        return (str(self.python), str(self.worker_script), self.model, str(self.hf_home or ""))

    # -- каталог ---------------------------------------------------------

    def list_voices(self) -> list[VoiceInfo]:
        if not self.installed():
            raise EngineError("Qwen3-TTS не установлен")
        return [
            VoiceInfo(key=key, name=key.replace("_", " "), gender=gender, language="ru",
                      tags=[f"родной: {native}", note])
            for key, gender, native, note in VOICES
        ]

    def supports_language(self, language: str) -> bool:
        return (language or "ru").lower()[:2] in {"ru", "en", "zh", "ja", "ko", "de", "fr", "pt", "es", "it"}

    def status(self) -> dict[str, Any]:
        base = {"name": self.name, "title": self.title, "needs_key": False, "variant": self.model}
        if not self.installed():
            return {**base, "ready": False, "installed": False,
                    "detail": f"не найден в {self.home}: нужно окружение с CUDA и модель (~9 ГБ)"}
        worker = _WORKERS.get(self._key())
        if worker and worker.alive():
            detail = f"модель загружена, {worker.device}"
        else:
            detail = "установлен; модель загрузится при первом синтезе (до минуты)"
        return {**base, "ready": True, "installed": True, "detail": detail}

    # -- синтез ----------------------------------------------------------

    def synthesize(
        self, text: str, voice_key: str, *, rate: float = 1.0,
        pitch: float = 1.0, volume: float = 1.0, emotion: str = "",
    ) -> tuple[bytes, str]:
        return self.synthesize_batch([{"text": text, "voice_key": voice_key, "emotion": emotion}])[0]

    def synthesize_batch(self, items: list[dict[str, Any]]) -> list[tuple[bytes, str]]:
        """Озвучить несколько реплик одним вызовом модели.

        Пачка из восьми идёт в пять раз быстрее, чем восемь по одной, — ради
        этого Qwen и пригоден для книг. Ошибка пачки — ошибка всех её реплик:
        озвучка главы тогда повторит их по одной.
        """
        known = {key for key, *_ in VOICES}
        for item in items:
            if not (item.get("text") or "").strip():
                raise EngineError("пустой текст реплики")
            if item["voice_key"] not in known:
                raise EngineFatalError(f"у Qwen3-TTS нет голоса {item['voice_key']!r}")

        from ... import paths

        scratch = paths.cache_dir() / "qwen"
        scratch.mkdir(parents=True, exist_ok=True)
        targets = [scratch / f"{uuid.uuid4().hex}.wav" for _ in items]
        answer = self._worker().request({"items": [
            {"text": item["text"].strip(), "speaker": item["voice_key"], "language": LANGUAGE,
             "instruct": INSTRUCTIONS.get(item.get("emotion", ""), ""), "out": str(target)}
            for item, target in zip(items, targets)
        ]})
        try:
            if not answer.get("ok"):
                raise EngineError(f"Qwen3-TTS: {answer.get('error', 'без причины')}")
            return [(target.read_bytes(), "wav") for target in targets]
        finally:
            for target in targets:
                target.unlink(missing_ok=True)


def engine(options: dict[str, Any] | None = None) -> QwenEngine:
    return QwenEngine(options)


def shutdown() -> None:
    """Остановить рабочие процессы — при выходе из приложения."""
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.stop()
