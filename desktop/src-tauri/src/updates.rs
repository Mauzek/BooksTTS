//! Обновления из GitHub Releases.
//!
//! Оболочка скачивает `latest.json` из последнего релиза, сверяет версию и
//! подпись пакета открытым ключом из tauri.conf.json. Пакет, подписанный
//! чужим ключом, не установится — даже если его подложат в релиз.

use serde::Serialize;
use tauri::{AppHandle, Emitter};
use tauri_plugin_updater::UpdaterExt;

#[derive(Serialize)]
pub struct UpdateInfo {
    version: String,
    current: String,
    notes: Option<String>,
    date: Option<String>,
}

#[derive(Clone, Serialize)]
struct Progress {
    downloaded: u64,
    total: Option<u64>,
}

#[tauri::command]
pub fn app_version(app: AppHandle) -> String {
    app.package_info().version.to_string()
}

#[tauri::command]
pub async fn check_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| format!("не удалось проверить обновления: {e}"))?;
    Ok(update.map(|u| UpdateInfo {
        version: u.version.clone(),
        current: u.current_version.clone(),
        notes: u.body.clone(),
        date: u.date.map(|d| d.to_string()),
    }))
}

#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| format!("не удалось проверить обновления: {e}"))?
        .ok_or_else(|| "обновлений нет".to_string())?;

    let mut downloaded = 0u64;
    let reporter = app.clone();
    let bytes = update
        .download(
            move |chunk, total| {
                downloaded += chunk as u64;
                let _ = reporter.emit("update-progress", Progress { downloaded, total });
            },
            || {},
        )
        .await
        .map_err(|e| format!("не удалось скачать обновление: {e}"))?;

    // Бэкенд держит базу и файлы в папке установки: установщик не смог бы их
    // заменить. Останавливаем его только сейчас — пока шла загрузка, приложение
    // работало как обычно.
    crate::backend::stop(&app);
    update
        .install(bytes)
        .map_err(|e| format!("не удалось установить обновление: {e}"))?;
    // В Windows установщик сам закрывает приложение; на остальных системах —
    // перезапускаемся на новую версию.
    app.restart();
}
