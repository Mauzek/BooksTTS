"""Заглушка Claude-клиента: тесты гоняются без ключа, без сети и без денег."""

from __future__ import annotations

import json
import re


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Response:
    stop_reason = "end_turn"
    usage = _Usage()

    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, outer: "FakeClient") -> None:
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        message = kwargs["messages"][0]["content"]
        return _Response(self.outer.reply(message, len(self.outer.calls)))


class _Model:
    def __init__(self, model_id: str) -> None:
        self.id = model_id
        self.display_name = model_id


class _Models:
    def list(self):
        return [_Model("claude-sonnet-5"), _Model("claude-opus-5")]


class FakeClient:
    """Клиент, который отдаёт то, что задано функцией ``reply``."""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.messages = _Messages(self)
        self.models = _Models()


_PARA_RE = re.compile(r"^\[(\d+)\] (.+)$", re.MULTILINE)


def targets(message: str) -> list[tuple[int, str]]:
    """Абзацы из раздела «АБЗАЦЫ» присланного промпта."""
    body = message.split("АБЗАЦЫ (разметить все):", 1)[-1]
    return [(int(n), t) for n, t in _PARA_RE.findall(body)]


def echo_narrator(message: str, _call: int) -> str:
    """Всё отдаёт рассказчику — проверяет механику, а не качество разметки."""
    items = [
        {"para": n, "speaker": "narrator", "text": t, "emotion": "нейтрально"}
        for n, t in targets(message)
    ]
    return json.dumps(items, ensure_ascii=False)


def two_speakers(message: str, _call: int) -> str:
    """Чётные абзацы — Аглая, нечётные — рассказчик: видно раскраску ролей."""
    items = [
        {
            "para": n,
            "speaker": "Аглая" if n % 2 == 0 else "narrator",
            "text": t,
            "emotion": "радостно" if n % 2 == 0 else "нейтрально",
        }
        for n, t in targets(message)
    ]
    return json.dumps(items, ensure_ascii=False)
