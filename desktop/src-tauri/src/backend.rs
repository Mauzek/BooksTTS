//! Python-бэкенд как дочерний процесс.
//!
//! Протокол простой: бэкенд слушает свободный порт и печатает в stdout строку
//! `BOOKTTS_READY {"port": N}`. Токен доступа уходит через переменную
//! окружения — аргументы процесса видны любому, окружение нет. Бэкенд сам
//! завершается, когда закрывается его stdin, поэтому сиротой не остаётся даже
//! при падении оболочки.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, Url};

const READY_PREFIX: &str = "BOOKTTS_READY ";
const STDERR_TAIL: usize = 60;
const GRACEFUL_STOP: Duration = Duration::from_secs(3);

#[derive(Clone, Serialize, Default)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum Status {
    #[default]
    Starting,
    Ready {
        url: String,
    },
    Failed {
        message: String,
    },
}

#[derive(Default)]
pub struct Backend {
    child: Mutex<Option<Child>>,
    status: Mutex<Status>,
    /// Выход штатный: завершение процесса — не ошибка.
    stopping: AtomicBool,
}

#[tauri::command]
pub fn backend_status(state: tauri::State<'_, Backend>) -> Status {
    state.status.lock().unwrap().clone()
}

#[tauri::command]
pub fn restart_backend(app: AppHandle) {
    stop(&app);
    spawn(app);
}

pub fn spawn(app: AppHandle) {
    std::thread::Builder::new()
        .name("backend".into())
        .spawn(move || run(app))
        .expect("не удалось запустить поток бэкенда");
}

/// Остановить бэкенд: сначала попросить (закрыть stdin), потом убить.
pub fn stop(app: &AppHandle) {
    let state = app.state::<Backend>();
    state.stopping.store(true, Ordering::SeqCst);
    let Some(mut child) = state.child.lock().unwrap().take() else {
        return;
    };
    drop(child.stdin.take());
    let deadline = std::time::Instant::now() + GRACEFUL_STOP;
    while std::time::Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn set_status(app: &AppHandle, status: Status) {
    *app.state::<Backend>().status.lock().unwrap() = status.clone();
    let _ = app.emit("backend-status", status);
}

fn token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("нет системного источника случайности");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Исходники проекта — для запуска бэкенда из Python в режиме разработки.
fn source_root() -> PathBuf {
    std::env::var_os("BOOKTTS_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
}

fn python_command(python: String) -> Command {
    let mut command = Command::new(python);
    command
        .current_dir(source_root())
        .args(["-m", "audiobook", "serve"]);
    command
}

fn backend_command(app: &AppHandle) -> Result<Command, String> {
    // Явная подмена: собранная оболочка против живых исходников.
    if let Ok(python) = std::env::var("BOOKTTS_PYTHON") {
        return Ok(python_command(python));
    }
    if cfg!(debug_assertions) {
        return Ok(python_command("python".into()));
    }
    let name = if cfg!(windows) {
        "booktts-backend.exe"
    } else {
        "booktts-backend"
    };
    let exe = app
        .path()
        .resource_dir()
        .map_err(|e| format!("не найдена папка ресурсов: {e}"))?
        .join("backend")
        .join(name);
    if !exe.is_file() {
        return Err(format!("не найден бэкенд: {}", exe.display()));
    }
    let mut command = Command::new(exe);
    command.arg("serve");
    Ok(command)
}

/// Адрес встроенной заставки: на неё возвращаемся, если бэкенд упал.
fn splash_url() -> Url {
    let raw = if cfg!(windows) {
        "http://tauri.localhost/index.html"
    } else {
        "tauri://localhost/index.html"
    };
    Url::parse(raw).expect("адрес заставки")
}

fn navigate(app: &AppHandle, url: Url) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.navigate(url);
    }
}

fn run(app: AppHandle) {
    let state = app.state::<Backend>();
    state.stopping.store(false, Ordering::SeqCst);
    set_status(&app, Status::Starting);

    let mut command = match backend_command(&app) {
        Ok(command) => command,
        Err(message) => return set_status(&app, Status::Failed { message }),
    };
    let token = token();
    command
        .args(["--port", "0", "--ready-line", "--exit-with-stdin"])
        .env("BOOKTTS_TOKEN", &token)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(e) => {
            return set_status(
                &app,
                Status::Failed {
                    message: format!("бэкенд не запустился: {e}"),
                },
            )
        }
    };
    let stdout = child.stdout.take().expect("stdout перехвачен");
    let stderr = child.stderr.take().expect("stderr перехвачен");
    *state.child.lock().unwrap() = Some(child);

    // stderr читаем отдельно: иначе переполненный буфер повесит бэкенд.
    let tail = Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL)));
    let tail_writer = Arc::clone(&tail);
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            eprintln!("[backend] {line}");
            let mut tail = tail_writer.lock().unwrap();
            if tail.len() == STDERR_TAIL {
                tail.pop_front();
            }
            tail.push_back(line);
        }
    });

    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        let Some(payload) = line.strip_prefix(READY_PREFIX) else {
            println!("[backend] {line}");
            continue;
        };
        let port = serde_json::from_str::<serde_json::Value>(payload)
            .ok()
            .and_then(|v| v.get("port").and_then(|p| p.as_u64()));
        match port {
            Some(port) => {
                let enter = format!("http://127.0.0.1:{port}/desktop/enter?token={token}");
                set_status(
                    &app,
                    Status::Ready {
                        url: format!("http://127.0.0.1:{port}/"),
                    },
                );
                navigate(&app, Url::parse(&enter).expect("адрес бэкенда"));
            }
            None => set_status(
                &app,
                Status::Failed {
                    message: format!("непонятная строка готовности: {line}"),
                },
            ),
        }
    }

    // stdout закрылся — процесс завершился.
    if state.stopping.load(Ordering::SeqCst) {
        return;
    }
    let code = state
        .child
        .lock()
        .unwrap()
        .take()
        .and_then(|mut child| child.wait().ok())
        .map(|status| format!(" (код {})", status.code().unwrap_or(-1)))
        .unwrap_or_default();
    // Дать потоку stderr дочитать последние строки.
    std::thread::sleep(Duration::from_millis(200));
    let log = tail.lock().unwrap().iter().cloned().collect::<Vec<_>>().join("\n");
    navigate(&app, splash_url());
    set_status(
        &app,
        Status::Failed {
            message: format!("бэкенд завершился{code}\n\n{log}"),
        },
    );
}
