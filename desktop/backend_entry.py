"""Точка входа собранного бэкенда: booktts-backend.exe serve …

Отдельный файл нужен PyInstaller-у: ``python -m audiobook`` в замороженном
приложении не существует, а здесь ``main`` вызывается напрямую.
"""

import multiprocessing
import sys

from audiobook.__main__ import main

if __name__ == "__main__":
    # torch и загрузчики данных порождают процессы; без этого замороженный
    # exe запускал бы сам себя заново вместо дочернего процесса.
    multiprocessing.freeze_support()
    sys.exit(main())
