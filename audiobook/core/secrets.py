"""Ключи API — в системном хранилище паролей, не в базе и не в открытом .env.

Значение ключа наружу не отдаётся никогда: интерфейс видит только признак
«ключ есть» и последние символы, чтобы отличить один ключ от другого.

Переменные окружения читаются, но только на чтение: если ключ лежит в .env,
приложение им воспользуется и предложит перенести его в хранилище.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SecretsError",
    "SERVICE_NAME",
    "SERVICES",
    "get_key",
    "set_key",
    "delete_key",
    "import_from_env",
    "status",
]

SERVICE_NAME = "BookTTS"


class SecretsError(RuntimeError):
    """Системное хранилище недоступно или ключ не сохранить."""


@dataclass(frozen=True)
class Service:
    name: str
    title: str
    env: str
    hint: str


SERVICES: dict[str, Service] = {
    "anthropic": Service(
        "anthropic", "Anthropic (разметка по ролям)", "ANTHROPIC_API_KEY",
        "Ключ или токен прокси, совместимого с Claude.",
    ),
    "elevenlabs": Service(
        "elevenlabs", "ElevenLabs (облачный синтез)", "ELEVENLABS_API_KEY",
        "Нужен только для голосов ElevenLabs. Silero работает без ключей.",
    ),
}


def _service(name: str) -> Service:
    service = SERVICES.get(name)
    if service is None:
        raise SecretsError(f"неизвестная служба {name!r}; есть: {', '.join(SERVICES)}")
    return service


def _keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover
        raise SecretsError(
            "нет библиотеки keyring: pip install -r requirements.txt"
        ) from exc
    return keyring


def _from_keyring(name: str) -> str:
    try:
        return _keyring().get_password(SERVICE_NAME, name) or ""
    except SecretsError:
        raise
    except Exception:  # noqa: BLE001 — на машине может не быть хранилища
        return ""


def get_key(name: str) -> str:
    """Ключ службы: сначала системное хранилище, потом окружение."""
    service = _service(name)
    return _from_keyring(service.name) or os.environ.get(service.env, "").strip()


def set_key(name: str, key: str) -> None:
    service = _service(name)
    key = (key or "").strip()
    if not key:
        raise SecretsError("пустой ключ")
    try:
        _keyring().set_password(SERVICE_NAME, service.name, key)
    except SecretsError:
        raise
    except Exception as exc:  # noqa: BLE001 — хранилище может быть недоступно
        raise SecretsError(f"не удалось сохранить ключ: {exc}") from exc


def delete_key(name: str) -> None:
    service = _service(name)
    try:
        _keyring().delete_password(SERVICE_NAME, service.name)
    except SecretsError:
        raise
    except Exception:  # noqa: BLE001 — ключа могло и не быть
        pass


def import_from_env(name: str) -> bool:
    """Перенести ключ из окружения в хранилище. False — в окружении пусто."""
    service = _service(name)
    value = os.environ.get(service.env, "").strip()
    if not value:
        return False
    set_key(service.name, value)
    return True


def _masked(key: str) -> str:
    """Хвост ключа — отличить один от другого, не показывая целиком."""
    return f"…{key[-4:]}" if len(key) > 8 else "…"


def status() -> list[dict[str, Any]]:
    """Состояние ключей для экрана настроек. Самих ключей здесь нет."""
    out = []
    for service in SERVICES.values():
        stored = _from_keyring(service.name)
        in_env = os.environ.get(service.env, "").strip()
        key = stored or in_env
        out.append({
            "service": service.name,
            "title": service.title,
            "hint": service.hint,
            "env": service.env,
            "present": bool(key),
            "source": "keyring" if stored else ("env" if in_env else ""),
            "masked": _masked(key) if key else "",
            "can_import_env": bool(in_env and not stored),
        })
    return out
