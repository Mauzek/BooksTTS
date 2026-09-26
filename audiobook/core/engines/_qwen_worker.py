"""Рабочий процесс Qwen3-TTS.

Запускается приложением в ОТДЕЛЬНОМ окружении Python — с CUDA-сборкой torch и
пакетом qwen-tts. Основное приложение с этим окружением не смешивается:
qwen-tts жёстко требует свою версию transformers, а torch с CUDA весит гигабайты.

Поэтому файл самодостаточен: ничего не импортирует из audiobook.

Протокол — по строке JSON на сообщение через stdin/stdout:

    <- {"ready": true, "device": "cuda:0", "model": "..."}         после загрузки модели
    <- {"ready": false, "error": "..."}                            модель не загрузилась
    -> {"id": 1, "text": "...", "speaker": "Ryan", "language": "Russian",
        "instruct": "", "out": "C:/.../x.wav"}
    <- {"id": 1, "ok": true, "seconds": 2.4, "spent": 1.1}
    <- {"id": 1, "ok": false, "error": "..."}

Процесс завершается, когда закрывается stdin: приложение закрылось или упало.
"""

import json
import sys
import time

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"

# Настоящий stdout — только для протокола. Подменяется в main(), а не при
# импорте: чужой импорт этого файла не должен ломать вывод импортирующего.
_PROTOCOL = None


def emit(message: dict) -> None:
    _PROTOCOL.write(json.dumps(message, ensure_ascii=False) + "\n")
    _PROTOCOL.flush()


def stub_sox() -> None:
    """Подставить заглушку вместо пакета sox, если он не импортируется.

    Пакет sox уже при импорте вызывает программу SoX, которой в Windows обычно
    нет. qwen-tts импортирует его ради токенизатора 25 Гц, а у моделей 12Hz
    токенизатор свой. Заглушка не даёт импорту упасть, а если её всё же позовут —
    падает с понятной причиной.
    """
    try:
        import sox  # noqa: F401
    except Exception:  # noqa: BLE001 — FileNotFoundError, OSError, что угодно
        import types

        class Transformer:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("этой модели нужна программа SoX — установите её в PATH")

        stub = types.ModuleType("sox")
        stub.Transformer = Transformer
        sys.modules["sox"] = stub


def load(model_name: str):
    stub_sox()
    # numba по умолчанию пишет кеш рядом с библиотекой; Python из Microsoft
    # Store там писать не может и зависает в бесконечных попытках.
    import os
    import tempfile

    os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(tempfile.gettempdir(), "booktts-qwen", "numba"))
    import torch
    from qwen_tts import Qwen3TTSModel

    cuda = torch.cuda.is_available()
    model = Qwen3TTSModel.from_pretrained(
        model_name,
        device_map="cuda:0" if cuda else "cpu",
        dtype=torch.bfloat16 if cuda else torch.float32,
        # sdpa, а не flash_attention_2: для Windows готовых сборок flash-attn нет.
        attn_implementation="sdpa",
    )
    return model, ("cuda:0" if cuda else "cpu")


def main() -> int:
    global _PROTOCOL
    # Всё, что печатают библиотеки (прогресс загрузки, предупреждения), уходит
    # в stderr и протокол не ломает.
    _PROTOCOL = sys.stdout
    sys.stdout = sys.stderr
    model_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    try:
        import soundfile

        model, device = load(model_name)
    except Exception as exc:  # noqa: BLE001 — сообщаем приложению любую причину
        emit({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    emit({"ready": True, "device": device, "model": model_name})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            # Пачка: одна фраза почти не нагружает видеокарту. Замерено на
            # RTX 3070: по одной — 0,15× реального времени, пачкой по 8 — 0,78×.
            items = request.get("items") or [request]
            started = time.time()
            wavs, rate = model.generate_custom_voice(
                text=[item["text"] for item in items],
                speaker=[item["speaker"] for item in items],
                language=[item.get("language") or "Russian" for item in items],
                instruct=[item.get("instruct") or "" for item in items],
            )
            results = []
            for item, audio in zip(items, wavs):
                soundfile.write(item["out"], audio, rate)
                results.append({"ok": True, "seconds": len(audio) / rate})
            emit({"id": request_id, "ok": True, "results": results,
                  "spent": time.time() - started})
        except Exception as exc:  # noqa: BLE001 — одна пачка не роняет процесс
            emit({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
