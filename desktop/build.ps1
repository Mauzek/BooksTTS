# Сборка установщика на этой машине — те же шаги, что в CI (.github/workflows/release.yml).
#
# Всё тяжёлое (бэкенд с torch, target/ Rust) собирается вне проекта: он лежит в
# OneDrive, и гигабайты артефактов там синхронизировались бы. Папка — из
# переменной BOOKTTS_BUILD_DIR (например, на несистемном диске), иначе
# %LOCALAPPDATA%\BookTTS-build.
#
# Ключ подписи обновлений: %USERPROFILE%\.tauri\booktts.key, пароль к нему —
# в хранилище паролей Windows (служба BookTTS-release).

# -SkipBackend — не пересобирать бэкенд (7 минут): для правок только в оболочке.
param([switch]$SkipBackend)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$build = $env:BOOKTTS_BUILD_DIR
if (-not $build) { $build = Join-Path $env:LOCALAPPDATA 'BookTTS-build' }
$env:CARGO_TARGET_DIR = Join-Path $build 'target'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

Push-Location $root
try {
    if ($SkipBackend) {
        Write-Host '1/3 бэкенд — пропущен, берётся уже собранный'
    } else {
        Write-Host '1/3 бэкенд (PyInstaller)'
        python -m PyInstaller desktop\backend.spec --noconfirm --log-level WARN `
            --distpath "$build\backend-dist" --workpath "$build\pyinstaller-work"
        if ($LASTEXITCODE) { throw 'бэкенд не собрался' }
    }

    Write-Host '2/3 проверка бэкенда'
    & "$build\backend-dist\booktts-backend\booktts-backend.exe" --version
    if ($LASTEXITCODE) { throw 'собранный бэкенд не запускается' }

    Write-Host '3/3 установщик (Tauri)'
    $env:TAURI_SIGNING_PRIVATE_KEY = Join-Path $env:USERPROFILE '.tauri\booktts.key'
    $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = python -c "import keyring; print(keyring.get_password('BookTTS-release', 'updater-key-password') or '')"
    # Путь к бэкенду здесь абсолютный — поэтому свой конфиг, а не tauri.release.json.
    $backend = ("$build\backend-dist\booktts-backend\" -replace '\\', '/')
    $config = @{ bundle = @{ resources = @{ $backend = 'backend/' } } } | ConvertTo-Json -Depth 5 -Compress
    $configFile = Join-Path $build 'tauri.local.json'
    # Без BOM: PowerShell 5 иначе допишет его, и Tauri не разберёт JSON.
    [IO.File]::WriteAllText($configFile, $config, (New-Object Text.UTF8Encoding $false))

    Push-Location (Join-Path $root 'desktop\src-tauri')
    try {
        cargo tauri build --config $configFile
        if ($LASTEXITCODE) { throw 'установщик не собрался' }
    } finally {
        Pop-Location
    }
    Write-Host "Готово: $env:CARGO_TARGET_DIR\release\bundle\nsis"
} finally {
    Pop-Location
    $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $null
}
