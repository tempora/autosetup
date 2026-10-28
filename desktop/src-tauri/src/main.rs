//! autosetup desktop shell: one window around the local autosetup server.
//!
//! On start it launches the bundled server (`server/autosetup-server.exe`, the Python
//! backend frozen by PyInstaller), shows a splash until `/api/health` answers, then points
//! the window at the dashboard. If an `autosetup serve` is already running on the port it
//! is used as is. The server keeps listening on the LAN, so the phone works as before.
//!
//! The server's stdin is a pipe from this process. The server exits when that pipe
//! closes, so it never outlives the window, even if the shell crashes.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent, Url, WebviewWindow};

const PORT: u16 = 8191;
const START_TIMEOUT: Duration = Duration::from_secs(90);

struct Server(Mutex<Option<Child>>);

/// True when the autosetup server answers `/api/health` on the port.
fn healthy() -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], PORT));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(300)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    if s.write_all(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").is_err() {
        return false;
    }
    let mut reply = String::new();
    let _ = s.read_to_string(&mut reply);
    reply.starts_with("HTTP/1.0 200") || reply.starts_with("HTTP/1.1 200")
}

/// Where the server's database lives (see autosetup.store.default_db_path); its log goes next to it.
fn data_dir() -> PathBuf {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(std::env::temp_dir).join("autosetup")
}

fn spawn_server(app: &AppHandle) -> Result<Option<Child>, String> {
    if healthy() {
        return Ok(None);
    }
    let exe = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("server")
        .join("autosetup-server.exe");
    let dir = data_dir();
    fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let log = File::create(dir.join("server.log")).map_err(|e| e.to_string())?;
    let mut cmd = Command::new(&exe);
    cmd.args(["serve", "--port", &PORT.to_string(), "--exit-with-stdin"])
        .stdin(Stdio::piped())
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn().map(Some).map_err(|e| format!("Couldn't start {}: {e}", exe.display()))
}

fn show_error(window: &WebviewWindow, text: &str) {
    let js = format!(
        "var m = document.getElementById('msg'); m.className = 'error'; m.textContent = {};",
        serde_json::to_string(text).unwrap_or_default()
    );
    let _ = window.eval(&js);
}

/// Wait for the server, then show the dashboard; report it if the server dies or never answers.
fn open_when_ready(app: AppHandle, window: WebviewWindow) {
    let start = Instant::now();
    while start.elapsed() < START_TIMEOUT {
        if healthy() {
            let url = Url::parse(&format!("http://127.0.0.1:{PORT}/")).expect("valid url");
            let _ = window.navigate(url);
            return;
        }
        let exited = app
            .state::<Server>()
            .0
            .lock()
            .ok()
            .and_then(|mut c| c.as_mut().and_then(|c| c.try_wait().ok().flatten()));
        if let Some(status) = exited {
            let log = data_dir().join("server.log");
            show_error(&window, &format!("The autosetup server stopped ({status}). See {}", log.display()));
            return;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    show_error(&window, "The autosetup server didn't start. See server.log in %LOCALAPPDATA%\\autosetup.");
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let window = app.get_webview_window("main").expect("main window");
            match spawn_server(&handle) {
                Ok(child) => {
                    app.manage(Server(Mutex::new(child)));
                    std::thread::spawn(move || open_when_ready(handle, window));
                }
                Err(e) => {
                    app.manage(Server(Mutex::new(None)));
                    show_error(&window, &e);
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building autosetup")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut child) = app.state::<Server>().0.lock().ok().and_then(|mut c| c.take()) {
                    drop(child.stdin.take()); // the server exits on EOF
                    if child.wait_timeout_ms(3000).is_none() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        });
}

trait WaitTimeout {
    fn wait_timeout_ms(&mut self, ms: u64) -> Option<std::process::ExitStatus>;
}

impl WaitTimeout for Child {
    fn wait_timeout_ms(&mut self, ms: u64) -> Option<std::process::ExitStatus> {
        let end = Instant::now() + Duration::from_millis(ms);
        while Instant::now() < end {
            if let Ok(Some(status)) = self.try_wait() {
                return Some(status);
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        None
    }
}
