//! Системные уведомления от имени BookTTS.
//!
//! Плагин уведомлений Tauri в сборке разработчика подписывается как
//! «Windows PowerShell», а нажатие на уведомление ни к чему не ведёт. Здесь
//! уведомление отправляется напрямую через WinRT со своим AppUserModelID:
//! имя и значок приложения Windows берёт из ключа реестра текущего
//! пользователя, ярлык в «Пуске» для этого не нужен. Нажатие поднимает окно
//! и открывает в нём экран, о котором уведомление.

use tauri::AppHandle;

#[cfg(windows)]
mod imp {
    use std::path::PathBuf;
    use std::sync::OnceLock;

    use tauri::{AppHandle, Emitter, Manager};
    use tauri_winrt_notification::{Duration, Toast};

    const DISPLAY_NAME: &str = "BookTTS";
    const ICON: &[u8] = include_bytes!("../icons/128x128.png");

    /// Зарегистрировать имя и значок один раз за запуск. Ошибка не мешает
    /// уведомлению: Windows покажет его, просто без красивой подписи.
    fn register(app: &AppHandle) -> &'static str {
        static APP_ID: OnceLock<String> = OnceLock::new();
        APP_ID.get_or_init(|| {
            let id = app.config().identifier.clone();
            if let Err(error) = write_registration(app, &id) {
                eprintln!("уведомления: не удалось зарегистрировать {id}: {error}");
            }
            id
        })
    }

    fn write_registration(app: &AppHandle, id: &str) -> Result<(), String> {
        let dir: PathBuf = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        let icon = dir.join("notification-icon.png");
        std::fs::write(&icon, ICON).map_err(|e| e.to_string())?;
        let key = windows_registry::CURRENT_USER
            .create(format!("Software\\Classes\\AppUserModelId\\{id}"))
            .map_err(|e| e.to_string())?;
        key.set_string("DisplayName", DISPLAY_NAME).map_err(|e| e.to_string())?;
        key.set_string("IconUri", icon.to_string_lossy()).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn show(app: &AppHandle, title: &str, body: &str, route: Option<String>) -> Result<(), String> {
        let id = register(app);
        let handle = app.clone();
        Toast::new(id)
            .title(title)
            .text1(body)
            .duration(Duration::Short)
            .on_activated(move |_action| {
                if let Some(window) = handle.get_webview_window("main") {
                    let _ = window.unminimize();
                    let _ = window.show();
                    let _ = window.set_focus();
                }
                if let Some(route) = &route {
                    let _ = handle.emit("notification-open", route.clone());
                }
                Ok(())
            })
            .show()
            .map_err(|e| format!("уведомление не показано: {e}"))
    }
}

/// Показать системное уведомление. `route` — экран интерфейса (`#/book/3`),
/// который откроется по нажатию.
#[tauri::command]
pub fn notify(app: AppHandle, title: String, body: String, route: Option<String>) -> Result<(), String> {
    #[cfg(windows)]
    {
        imp::show(&app, &title, &body, route)
    }
    #[cfg(not(windows))]
    {
        let _ = (app, title, body, route);
        Err("системные уведомления оболочки есть только в Windows".into())
    }
}
