# Запуск десктопной оболочки в режиме разработки.
#
# Бэкенд берётся из исходников (python -m audiobook serve), так что правки
# Python подхватываются перезапуском окна без пересборки Rust.
#
# target/ Tauri весит гигабайты, а проект лежит в OneDrive — сборку уводим
# в локальную папку, иначе OneDrive начнёт синхронизировать артефакты.

$ErrorActionPreference = 'Stop'
if (-not $env:CARGO_TARGET_DIR) {
    $env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA 'BookTTS-build\target'
}
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

Push-Location (Join-Path $PSScriptRoot 'src-tauri')
try {
    cargo run @args
} finally {
    Pop-Location
}
