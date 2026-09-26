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
import shutil
import subprocess
import tempfile
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


UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BookTTS"
HOME_NAME = "qwen"  # папка рядом с установленной программой


def legacy_home() -> Path:
    """Где Qwen лежал раньше: %LOCALAPPDATA%\BookTTS-qwen на системном диске."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "BookTTS-qwen"


def program_dir() -> Path | None:
    """Папка установленного приложения — рядом с ней держим тяжёлые данные.

    В собранном приложении это папка над backend\booktts-backend.exe; при
    запуске из исходников — место установки из реестра, если приложение стоит.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            location = str(winreg.QueryValueEx(key, "InstallLocation")[0]).strip().strip('"')
    except OSError:
        return None
    return Path(location) if location and Path(location).is_dir() else None


def suggested_home() -> Path | None:
    """Куда предлагать перенести Qwen: рядом с программой."""
    folder = program_dir()
    return folder / HOME_NAME if folder else None


def default_home() -> Path:
    """Папка Qwen, если в настройках не указана своя.

    Рядом с программой, если Qwen там уже есть; иначе — прежнее место, где он
    мог остаться от старых версий; для новой установки — снова рядом с программой.
    """
    beside = suggested_home()
    if beside and python_in(beside).is_file():
        return beside
    legacy = legacy_home()
    if python_in(legacy).is_file():
        return legacy
    return beside or legacy


def store_python(home: Path) -> bool:
    """Окружение построено на Python из Microsoft Store.

    Такой Python не может создавать файлы в %LOCALAPPDATA% — Windows
    перенаправляет запись, и numba внутри qwen-tts бесконечно пытается создать
    временный файл. Окружение нужно держать на другом диске или вне AppData.
    """
    try:
        config = (home / "venv" / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "WindowsApps" in config or "PythonSoftwareFoundation" in config


def inside_appdata(path: Path) -> bool:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return False
    try:
        path.resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def scratch_dir() -> Path:
    """Куда рабочий процесс кладёт готовые WAV и кеш numba.

    Временная папка Windows не перенаправляется даже для Python из Store:
    файл, записанный рабочим, приложение увидит там же.
    """
    folder = Path(tempfile.gettempdir()) / "booktts-qwen"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


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
        # Кеш numba — во временную папку: рядом с библиотеками (по умолчанию)
        # Python из Store писать не может и зависает в бесконечных попытках.
        environment["NUMBA_CACHE_DIR"] = str(scratch_dir() / "numba")
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
        # Ссылки раскрываем: папку, перенесённую на другой диск через junction
        # в AppData, Python из Store иначе видит «через» AppData и не может в неё писать.
        self.home = Path(options.get("engine.qwen.home") or default_home()).expanduser().resolve()
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
        base = {"name": self.name, "title": self.title, "needs_key": False, "variant": self.model,
                "home": str(self.home)}
        if not self.installed():
            return {**base, "ready": False, "installed": False,
                    "detail": f"не найден в {self.home}: нужно окружение с CUDA и модель (~9 ГБ)"}
        if store_python(self.home) and inside_appdata(self.home):
            return {**base, "ready": False, "installed": True, "needs_move": True,
                    "detail": "папка Qwen лежит в AppData, а Python из Microsoft Store не может туда писать — "
                              "перенесите её в «Настройках» (Озвучка → Папка Qwen3-TTS)"}
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

        targets = [scratch_dir() / f"{uuid.uuid4().hex}.wav" for _ in items]
        answer = self._worker().request({"items": [
            {"text": item["text"].strip(), "speaker": item["voice_key"], "language": LANGUAGE,
             "instruct": INSTRUCTIONS.get(item.get("emotion", ""), ""), "out": str(target)}
            for item, target in zip(items, targets)
        ]})
        try:
            if not answer.get("ok"):
                raise EngineError(f"Qwen3-TTS: {answer.get('error', 'без причины')}")
            results = [(target.read_bytes(), "wav") for target in targets]
        finally:
            for target in targets:
                target.unlink(missing_ok=True)
        # Модель иногда «заговаривается»: вместо фразы — десяток секунд
        # бормотания. Такую фразу пишем заново, по одной.
        for index, item in enumerate(items):
            for _attempt in range(RUNAWAY_RETRIES):
                if not runaway(results[index][0], item["text"]):
                    break
                retry = self._single(item)
                if wav_seconds(retry[0]) < wav_seconds(results[index][0]):
                    results[index] = retry
        return results

    def _single(self, item: dict[str, Any]) -> tuple[bytes, str]:
        target = scratch_dir() / f"{uuid.uuid4().hex}.wav"
        answer = self._worker().request({"items": [
            {"text": item["text"].strip(), "speaker": item["voice_key"], "language": LANGUAGE,
             "instruct": INSTRUCTIONS.get(item.get("emotion", ""), ""), "out": str(target)}
        ]})
        try:
            if not answer.get("ok"):
                raise EngineError(f"Qwen3-TTS: {answer.get('error', 'без причины')}")
            return target.read_bytes(), "wav"
        finally:
            target.unlink(missing_ok=True)


RUNAWAY_RETRIES = 2
# Русская речь — около 14 знаков в секунду. Втрое медленнее и с запасом
# на паузы — это уже не чтение фразы, а сорвавшаяся генерация.
SECONDS_PER_CHAR = 0.14
SLACK_SECONDS = 2.0


def wav_seconds(data: bytes) -> float:
    import io
    import wave

    try:
        with wave.open(io.BytesIO(data)) as handle:
            return handle.getnframes() / float(handle.getframerate() or 1)
    except (wave.Error, EOFError):
        return 0.0


def runaway(data: bytes, text: str) -> bool:
    """Звук слишком длинный для такой фразы — модель сорвалась."""
    return wav_seconds(data) > len((text or "").strip()) * SECONDS_PER_CHAR + SLACK_SECONDS


def engine(options: dict[str, Any] | None = None) -> QwenEngine:
    return QwenEngine(options)


def shutdown() -> None:
    """Остановить рабочие процессы — при выходе из приложения."""
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.stop()


# --------------------------------------------------------------------------
# Перенос папки Qwen
# --------------------------------------------------------------------------

RESERVE_BYTES = 512 * 1024 * 1024  # запас на диске сверх размера папки


def folder_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def relocate(source: Path, target: Path, *, on_progress=None, should_stop=None) -> dict[str, Any]:
    """Перенести папку Qwen (окружение и модель) целиком, например на другой диск.

    Сначала копия, потом проверка, и только потом удаление старой папки: если
    перенос прервать, рабочая копия остаётся на старом месте.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if not python_in(source).is_file():
        raise EngineError(f"в {source} нет окружения Qwen — переносить нечего")
    if target == source:
        raise EngineError("Qwen уже лежит в этой папке")
    if source in target.parents:
        raise EngineError("нельзя перенести папку внутрь неё самой")
    if target.exists() and any(target.iterdir()):
        raise EngineError(f"папка {target} не пуста — выберите пустую или новую")

    files = [f for f in source.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    free = shutil.disk_usage(_existing_parent(target)).free
    if free < total + RESERVE_BYTES:
        gb = lambda n: f"{n / 1024 ** 3:.1f}".replace(".", ",")  # noqa: E731
        raise EngineError(f"на диске {target.anchor} свободно {gb(free)} ГБ, а нужно {gb(total)} ГБ")

    shutdown()  # модель держит файлы открытыми
    target.mkdir(parents=True, exist_ok=True)
    for folder in (d for d in source.rglob("*") if d.is_dir()):
        (target / folder.relative_to(source)).mkdir(parents=True, exist_ok=True)
    copied = 0
    for index, file in enumerate(files):
        if should_stop and should_stop():
            shutil.rmtree(target, ignore_errors=True)
            return {"cancelled": True, "home": str(source)}
        destination = target / file.relative_to(source)
        shutil.copy2(file, destination)
        copied += file.stat().st_size
        if on_progress and (index % 50 == 0 or index == len(files) - 1):
            on_progress(copied, total)

    if not python_in(target).is_file() or folder_size(target) < total:
        raise EngineError("копия получилась неполной — старая папка оставлена на месте")
    shutil.rmtree(source, ignore_errors=True)
    # Ссылка из AppData на старую папку теперь ведёт в никуда — убираем её.
    link = legacy_home()
    if link != source and os.path.realpath(link) == str(source):
        try:
            os.rmdir(link)
        except OSError:
            pass
    left = source.exists()
    return {"home": str(target), "bytes": total, "left_behind": str(source) if left else ""}
