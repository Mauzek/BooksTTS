//! Оболочка BookTTS.
//!
//! Rust здесь тонкий: запускает Python-бэкенд, открывает окно на его адресе
//! и гасит бэкенд при выходе. Вся логика — в `audiobook/core`.

mod backend;
mod notify;
mod updates;

use tauri::{Manager, RunEvent};

pub fn run() {
    let app = tauri::Builder::default()
        // Второй экземпляр означал бы второй бэкенд на той же базе и двойную
        // обработку очереди синтеза. Вместо этого поднимаем уже открытое окно.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(backend::Backend::default())
        .setup(|app| {
            backend::spawn(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend::backend_status,
            backend::restart_backend,
            notify::notify,
            updates::app_version,
            updates::check_update,
            updates::install_update
        ])
        .build(tauri::generate_context!())
        .expect("не удалось собрать приложение");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            backend::stop(handle);
        }
    });
}
