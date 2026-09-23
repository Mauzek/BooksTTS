# Сборка Python-бэкенда в папку с exe (onedir): torch весит сотни мегабайт,
# и распаковывать его во временную папку при каждом запуске (onefile) — это
# лишние десятки секунд старта.
#
#   python -m PyInstaller desktop/backend.spec --noconfirm --distpath <куда>

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH задаёт сам PyInstaller

hiddenimports = (
    # Движки находятся через pkgutil.iter_modules — статический анализ их не видит.
    collect_submodules("audiobook")
    + collect_submodules("uvicorn")
    # Silero исполняет hubconf.py из скачанного репозитория, а тот импортирует
    # вот это — в exe оно должно быть заранее.
    + ["torchaudio", "omegaconf", "yaml"]
    # Хранилище паролей Windows для ключей API.
    + ["keyring.backends.Windows", "win32ctypes.core"]
)

datas = (
    [(str(ROOT / "audiobook" / "web"), "audiobook/web")]
    # Рабочий скрипт Qwen исполняет ВНЕШНИЙ Python из окружения с CUDA —
    # ему нужен настоящий файл на диске, а не модуль внутри архива PyInstaller.
    + [(str(ROOT / "audiobook" / "core" / "engines" / "_qwen_worker.py"), "audiobook/core/engines")]
    + collect_data_files("imageio_ffmpeg")  # ffmpeg.exe для mp3 и m4b
    + collect_data_files("docx")  # шаблоны python-docx
    + collect_data_files("certifi")  # корневые сертификаты для HTTPS
    + copy_metadata("keyring")  # keyring ищет бэкенды через entry points
)

a = Analysis(  # noqa: F821
    [str(ROOT / "desktop" / "backend_entry.py")],
    pathex=[str(ROOT)],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "openpyxl", "pptx", "xlsxwriter"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="booktts-backend",
    # Консольный: оболочка общается с бэкендом через stdin/stdout, а окно
    # консоли она сама прячет флагом CREATE_NO_WINDOW.
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="booktts-backend", upx=False)  # noqa: F821
