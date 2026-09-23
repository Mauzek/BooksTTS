"""Qwen3-TTS через локальный Gradio.

Сервер поднимает пользователь сам (``qwen-tts-demo …``), поэтому набор голосов
и имена эндпоинтов спрашиваем у самого сервера через ``/gradio_api/info``, а не
зашиваем в код. Встроенные голоса моделей CustomVoice (сверено 18.09.2026 по
README проекта) используются только как запасной список, если сервер про свои
варианты не рассказал.
"""

from __future__ import annotations

from typing import Any

from . import EngineError, VoiceInfo

NAME = "qwen"
TITLE = "Qwen3-TTS (локальный сервер)"

DEFAULT_URL = "http://127.0.0.1:7860"
TIMEOUT = 180.0  # синтез длинной реплики
# Опрос доступности не должен держать экран голосов: сервера может не быть.
INFO_TIMEOUT = 5.0

# Запасной список: девять предустановленных голосов CustomVoice.
FALLBACK_VOICES = [
    ("Vivian", "ж", "zh"), ("Serena", "ж", "zh"), ("Uncle_Fu", "м", "zh"),
    ("Dylan", "м", "zh"), ("Eric", "м", "zh"), ("Ryan", "м", "en"),
    ("Aiden", "м", "en"), ("Ono_Anna", "ж", "ja"), ("Sohee", "ж", "ko"),
]
# Русский у Qwen3-TTS поддержан наравне с остальными девятью языками.
LANGUAGES = ("zh", "en", "ru", "de", "fr", "ja", "ko", "pt", "es", "it")

_VOICE_HINTS = ("voice", "speaker", "spk", "голос")
_TTS_HINTS = ("tts", "generate", "synth", "speech", "predict")


class QwenEngine:
    name = NAME
    title = TITLE

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        self.url = str(options.get("engine.qwen.url") or DEFAULT_URL).rstrip("/")
        self.api_name = options.get("engine.qwen.api_name") or ""
        self._transport = options.get("engine.qwen.transport")  # подмена в тестах

    # -- связь -----------------------------------------------------------

    def _request(self, method: str, path: str, timeout: float = TIMEOUT, **kwargs) -> Any:
        if self._transport is not None:
            return self._transport(method, path, **kwargs)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise EngineError("нужен httpx: pip install -r requirements.txt") from exc
        try:
            return httpx.request(method, f"{self.url}{path}", timeout=timeout, **kwargs)
        except Exception as exc:  # noqa: BLE001 — сеть
            raise EngineError(
                f"сервер Qwen3-TTS не отвечает на {self.url} — запущен ли он?"
            ) from exc

    def _info(self) -> dict[str, Any]:
        last: Exception | None = None
        for path in ("/gradio_api/info", "/info"):
            try:
                response = self._request("GET", path, timeout=INFO_TIMEOUT)
            except EngineError as exc:
                last = exc
                continue
            if getattr(response, "status_code", 500) < 400:
                try:
                    return response.json()
                except Exception:  # noqa: BLE001 — не тот сервер
                    continue
        if last is not None:
            raise last
        raise EngineError(f"по адресу {self.url} не видно Gradio-интерфейса")

    @staticmethod
    def _endpoints(info: dict[str, Any]) -> dict[str, Any]:
        named = (info.get("named_endpoints") or {}) if isinstance(info, dict) else {}
        return {name: spec for name, spec in named.items() if isinstance(spec, dict)}

    def _pick_endpoint(self, info: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        endpoints = self._endpoints(info)
        if not endpoints:
            raise EngineError("у сервера нет ни одного API-эндпоинта")
        if self.api_name:
            spec = endpoints.get(self.api_name)
            if spec is None:
                raise EngineError(
                    f"на сервере нет эндпоинта {self.api_name!r}; есть: {', '.join(endpoints)}"
                )
            return self.api_name, spec
        for name, spec in endpoints.items():
            if any(hint in name.lower() for hint in _TTS_HINTS):
                return name, spec
        name = next(iter(endpoints))
        return name, endpoints[name]

    @staticmethod
    def _choices(spec: dict[str, Any]) -> list[str]:
        """Варианты выпадающего списка голосов из описания эндпоинта."""
        for parameter in spec.get("parameters", []) or []:
            label = f"{parameter.get('label', '')} {parameter.get('parameter_name', '')}".lower()
            if not any(hint in label for hint in _VOICE_HINTS):
                continue
            enum = ((parameter.get("type") or {}).get("enum")
                    or (parameter.get("python_type") or {}).get("enum")
                    or parameter.get("choices"))
            if enum:
                return [str(v) for v in enum]
        return []

    # -- каталог ---------------------------------------------------------

    def list_voices(self) -> list[VoiceInfo]:
        """Голоса запущенного сервера.

        Если сервера нет — это ошибка, а не пустой список: положить в каталог
        встроенные имена значило бы предложить голоса, которыми не озвучить.
        """
        info = self._info()  # сервер недоступен -> EngineError наверх
        name, spec = self._pick_endpoint(info)
        choices = self._choices(spec)
        if choices:
            return [VoiceInfo(key=v, name=v, language="", tags=[name] if name else [])
                    for v in choices]
        # Сервер отвечает, но про свои голоса не рассказал — берём встроенные.
        return [
            VoiceInfo(key=key, name=key, gender=gender, language=language, tags=["встроенный"])
            for key, gender, language in FALLBACK_VOICES
        ]

    def supports_language(self, language: str) -> bool:
        return (language or "ru").lower()[:2] in LANGUAGES

    def status(self) -> dict[str, Any]:
        base = {"name": self.name, "title": self.title, "needs_key": False, "variant": self.url}
        try:
            info = self._info()
            name, _spec = self._pick_endpoint(info)
        except EngineError as exc:
            return {**base, "ready": False, "detail": str(exc)}
        return {**base, "ready": True, "detail": f"{self.url}, эндпоинт {name}"}

    # -- синтез ----------------------------------------------------------

    def synthesize(
        self, text: str, voice_key: str, *, rate: float = 1.0,
        pitch: float = 1.0, volume: float = 1.0,
    ) -> tuple[bytes, str]:
        text = (text or "").strip()
        if not text:
            raise EngineError("пустой текст реплики")
        name, _spec = self._pick_endpoint(self._info())
        started = self._request("POST", f"/gradio_api/call{name}", json={"data": [text, voice_key]})
        try:
            event_id = started.json().get("event_id")
        except Exception as exc:  # noqa: BLE001
            raise EngineError("сервер Qwen3-TTS ответил не тем, чего ждали") from exc
        if not event_id:
            raise EngineError("сервер Qwen3-TTS не выдал идентификатор задачи")

        result = self._request("GET", f"/gradio_api/call{name}/{event_id}")
        path = _audio_path(getattr(result, "text", "") or "")
        if not path:
            raise EngineError("сервер Qwen3-TTS не вернул аудио")
        downloaded = self._request("GET", f"/gradio_api/file={path}")
        data = getattr(downloaded, "content", b"")
        if not data:
            raise EngineError("файл с озвучкой пуст")
        return data, "wav" if path.lower().endswith(".wav") else "mp3"


def _audio_path(stream: str) -> str:
    """Вытащить путь к файлу из потока событий Gradio."""
    import json
    import re

    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except ValueError:
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if isinstance(item, dict):
                candidate = item.get("path") or item.get("name") or item.get("url")
                if candidate:
                    return str(candidate)
            if isinstance(item, str) and re.search(r"\.(wav|mp3|flac|ogg)$", item, re.I):
                return item
    return ""


def engine(options: dict[str, Any] | None = None) -> QwenEngine:
    return QwenEngine(options)
