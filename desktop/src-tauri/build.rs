fn main() {
    // Интерфейс окну отдаёт бэкенд с http://127.0.0.1 — для Tauri это
    // удалённая страница, и собственные команды приложения ей по умолчанию
    // запрещены («not allowed by ACL»). Объявляем их здесь: tauri-build
    // сгенерирует разрешения allow-<команда-через-дефис>, а capabilities/default.json
    // выдаёт их окну. Новую команду нужно добавить и сюда, и туда.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "backend_status",
            "restart_backend",
            "notify",
            "app_version",
            "check_update",
            "install_update",
        ]),
    ))
    .expect("tauri-build не отработал");
}
