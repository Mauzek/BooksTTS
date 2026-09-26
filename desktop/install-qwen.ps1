# Установка Qwen3-TTS: отдельное окружение с CUDA-сборкой torch и моделью.
#
# Нужны видеокарта NVIDIA (6 ГБ памяти или больше), Python 3.11 и около 10 ГБ
# на диске. Окружение ставится рядом с установленным приложением (папка qwen),
# там его ищет приложение; без установленного приложения — в
# %LOCALAPPDATA%\BookTTS-qwen. Основное приложение с ним не смешивается:
# qwen-tts требует свою версию transformers, а torch с CUDA весит гигабайты.
#
# Python из Microsoft Store не может писать в %LOCALAPPDATA% (Windows
# перенаправляет запись) — с ним окружение должно лежать вне AppData.
#
#   powershell -ExecutionPolicy Bypass -File desktop\install-qwen.ps1
#
# -Model Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice — облегчённая модель для карт с малой памятью.
# -Root E:\programs\llm\BookTTS-qwen — поставить на другой диск; ту же папку укажите
#       в приложении: Настройки -> Папка Qwen3-TTS.

param(
    [string]$Model = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice',
    [string]$Root = ''
)

$ErrorActionPreference = 'Stop'
if (-not $Root) {
    $installed = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BookTTS' -ErrorAction SilentlyContinue).InstallLocation
    if ($installed) { $Root = Join-Path $installed.Trim('"') 'qwen' }
    else { $Root = Join-Path $env:LOCALAPPDATA 'BookTTS-qwen' }
}
Write-Host "Папка Qwen: $Root"
$root = $Root
$python = Join-Path $root 'venv\Scripts\python.exe'
New-Item -ItemType Directory -Force $root | Out-Null

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Warning 'nvidia-smi не найден: без видеокарты NVIDIA Qwen3-TTS будет мучительно медленным.'
}

if (-not (Test-Path $python)) {
    Write-Host '1/4 окружение'
    python -m venv (Join-Path $root 'venv')
    if ($LASTEXITCODE) { throw 'не удалось создать окружение: нужен Python 3.11' }
}

Write-Host '2/4 torch с CUDA (около 2,5 ГБ)'
& $python -m pip install --disable-pip-version-check torch torchaudio --index-url https://download.pytorch.org/whl/cu126
if ($LASTEXITCODE) { throw 'torch не установился' }

Write-Host '3/4 qwen-tts'
# hf_xet заметно ускоряет скачивание модели с Hugging Face.
& $python -m pip install --disable-pip-version-check qwen-tts hf_xet
if ($LASTEXITCODE) { throw 'qwen-tts не установился' }

& $python -c "import torch, sys; ok = torch.cuda.is_available(); print('CUDA:', ok, torch.cuda.get_device_name(0) if ok else ''); sys.exit(0 if ok else 1)"
if ($LASTEXITCODE) { Write-Warning 'torch не видит видеокарту — синтез пойдёт на процессоре, очень медленно.' }

Write-Host "4/4 модель $Model (несколько гигабайт)"
# Модель — в папку Qwen, рядом с окружением: приложение найдёт её там же.
$env:HF_HOME = Join-Path $root 'huggingface'
& $python -c "from huggingface_hub import snapshot_download; import sys; print(snapshot_download(sys.argv[1]))" $Model
if ($LASTEXITCODE) { throw 'модель не скачалась' }

Write-Host ''
Write-Host 'Готово. В приложении: Голоса -> Qwen3-TTS -> Обновить каталог.'
