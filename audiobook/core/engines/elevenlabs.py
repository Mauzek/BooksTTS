"""ElevenLabs: облачный синтез по ключу пользователя.

Эндпоинты сверены 18.09.2026 по справочнику SDK: каталог — ``GET /v2/voices``
с постраничной выдачей (старый ``/v1/voices`` отваливается, когда голосов
больше пятисот), синтез — ``POST /v1/text-to-speech/{voice_id}``, остаток
квоты — ``GET /v1/user``.

Список голосов не зашит в код: он приходит из аккаунта пользователя.
"""

from __future__ import annotations

from typing import Any

from .. import secrets
from . import EngineError, EngineFatalError, VoiceInfo

NAME = "elevenlabs"
TITLE = "ElevenLabs (облако)"

API = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
# mp3 доступен на любом тарифе, в отличие от pcm/wav.
DEFAULT_FORMAT = "mp3_44100_128"
PAGE_SIZE = 100
TIMEOUT = 60.0

_GENDER = {"male": "м", "female": "ж", "мужской": "м", "женский": "ж"}


class ElevenLabsEngine:
    name = NAME
    title = TITLE

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        self.model = options.get("engine.elevenlabs.model") or DEFAULT_MODEL
        self.output_format = options.get("engine.elevenlabs.format") or DEFAULT_FORMAT
        self._client_factory = options.get("engine.elevenlabs.client")  # подмена в тестах

    # -- связь -----------------------------------------------------------

    def _key(self) -> str:
        key = secrets.get_key("elevenlabs")
        if not key:
            raise EngineFatalError(
                "нет ключа ElevenLabs — добавьте его в настройках или используйте Silero"
            )
        return key

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if self._client_factory is not None:
            return self._client_factory(method, path, **kwargs)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise EngineError("нужен httpx: pip install -r requirements.txt") from exc
        try:
            response = httpx.request(
                method, f"{API}{path}", timeout=TIMEOUT,
                headers={"xi-api-key": self._key()}, **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — сеть
            raise EngineError(f"ElevenLabs недоступен: {exc}") from exc
        if response.status_code == 401:
            raise EngineFatalError("ElevenLabs не принял ключ")
        if response.status_code == 429 or response.status_code >= 500:
            # Лимит запросов и сбой на их стороне проходят сами — повтор уместен.
            raise EngineError(f"ElevenLabs ответил {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise EngineFatalError(
                f"ElevenLabs ответил {response.status_code}: {response.text[:200]}"
            )
        return response

    # -- каталог ---------------------------------------------------------

    def list_voices(self) -> list[VoiceInfo]:
        voices: list[VoiceInfo] = []
        token = None
        for _page in range(20):  # предохранитель от бесконечной постраничности
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if token:
                params["next_page_token"] = token
            payload = self._request("GET", "/v2/voices", params=params).json()
            for item in payload.get("voices", []):
                labels = item.get("labels") or {}
                tags = [str(v) for v in (labels.get("accent"), labels.get("age"),
                                         labels.get("use_case"), item.get("category")) if v]
                voices.append(VoiceInfo(
                    key=item.get("voice_id", ""),
                    name=item.get("name") or item.get("voice_id", ""),
                    gender=_GENDER.get(str(labels.get("gender", "")).lower(), ""),
                    language=str(labels.get("language") or item.get("fine_tuning", {}).get("language") or ""),
                    tags=tags,
                ))
            token = payload.get("next_page_token")
            if not token:
                break
        return [v for v in voices if v.key]

    def supports_language(self, language: str) -> bool:
        return True  # multilingual-модели тянут и русский, и остальное

    def status(self) -> dict[str, Any]:
        base = {"name": self.name, "title": self.title, "needs_key": True,
                "variant": self.model}
        if not secrets.get_key("elevenlabs"):
            return {**base, "ready": False, "detail": "нет ключа"}
        try:
            user = self._request("GET", "/v1/user").json()
        except EngineError as exc:
            return {**base, "ready": False, "detail": str(exc)}
        subscription = user.get("subscription") or {}
        used = subscription.get("character_count")
        limit = subscription.get("character_limit")
        detail = f"тариф {subscription.get('tier', '—')}"
        if isinstance(used, int) and isinstance(limit, int) and limit:
            detail += f", осталось {limit - used} из {limit} символов"
        return {**base, "ready": True, "detail": detail,
                "quota": {"used": used, "limit": limit}}

    # -- синтез ----------------------------------------------------------

    def synthesize(
        self, text: str, voice_key: str, *, rate: float = 1.0,
        pitch: float = 1.0, volume: float = 1.0,
    ) -> tuple[bytes, str]:
        text = (text or "").strip()
        if not text:
            raise EngineError("пустой текст реплики")
        settings: dict[str, Any] = {}
        if abs(rate - 1.0) >= 1e-3:
            # У ElevenLabs скорость задаётся в узком диапазоне; за него не выходим.
            settings["speed"] = max(0.7, min(1.2, float(rate)))
        body: dict[str, Any] = {"text": text, "model_id": self.model}
        if settings:
            body["voice_settings"] = settings
        response = self._request(
            "POST", f"/v1/text-to-speech/{voice_key}",
            params={"output_format": self.output_format}, json=body,
        )
        data = response.content
        if not data:
            raise EngineError("ElevenLabs вернул пустой ответ")
        return data, "mp3" if self.output_format.startswith("mp3") else "wav"


def engine(options: dict[str, Any] | None = None) -> ElevenLabsEngine:
    return ElevenLabsEngine(options)
