// DemoQueryCS2 desktop shell: native window + Python engine sidecar.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use tauri::{Manager, RunEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct ServerProc(Mutex<Option<Child>>);

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8642)
}

fn health_ok(port: u16) -> bool {
    let Ok(mut s) = TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_millis(800)));
    let req = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 32];
    match s.read(&mut buf) {
        Ok(n) if n > 0 => String::from_utf8_lossy(&buf[..n]).contains("200"),
        _ => false,
    }
}

fn spawn_server(exe: &std::path::Path, port: u16) -> std::io::Result<Child> {
    let mut cmd = Command::new(exe);
    cmd.args(["--headless", "--port", &port.to_string()]);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.spawn()
}

fn kill_tree(child: &mut Child) {
    // taskkill /T also reaps the scanner's multiprocessing workers
    #[cfg(windows)]
    {
        let pid = child.id().to_string();
        let mut cmd = Command::new("taskkill");
        cmd.args(["/PID", &pid, "/T", "/F"]);
        cmd.creation_flags(CREATE_NO_WINDOW);
        let _ = cmd.status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn update_check_enabled() -> bool {
    if std::env::var("CS2SF_NO_UPDATE_CHECK").ok().as_deref() == Some("1") {
        return false;
    }
    // honor the sidecar's "check for updates" setting; mirror its data-dir
    // chain: CS2SF_DATA_DIR -> %LOCALAPPDATA%\DemoQueryCS2 -> ~\DemoQueryCS2
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(d) = std::env::var("CS2SF_DATA_DIR") {
        candidates.push(d.into());
    }
    if let Ok(l) = std::env::var("LOCALAPPDATA") {
        candidates.push(std::path::Path::new(&l).join("DemoQueryCS2"));
    }
    if let Ok(h) = std::env::var("USERPROFILE") {
        candidates.push(std::path::Path::new(&h).join("DemoQueryCS2"));
    }
    for dir in candidates {
        if let Ok(text) = std::fs::read_to_string(dir.join("settings.json")) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                return v["ui"]["check_updates"] != serde_json::Value::Bool(false);
            }
        }
        if dir.exists() {
            break; // data dir found but no readable settings: default on
        }
    }
    true
}

fn offer_update(app: tauri::AppHandle) {
    if !update_check_enabled() {
        return;
    }
    std::thread::spawn(move || {
        // test builds can point at a local manifest via CS2SF_UPDATE_URL
        let builder = app.updater_builder();
        let builder = match std::env::var("CS2SF_UPDATE_URL").ok().and_then(|u| u.parse().ok()) {
            Some(url) => match builder.endpoints(vec![url]) {
                Ok(b) => b,
                Err(_) => return,
            },
            None => builder,
        };
        let Ok(updater) = builder.build() else { return };
        let update = match tauri::async_runtime::block_on(updater.check()) {
            Ok(Some(u)) => u,
            _ => return, // up to date, offline or bad manifest: stay quiet
        };
        let msg = format!(
            "DemoQueryCS2 {} is available (you have {}).\n\nInstall now? \
             The app will restart; your demo library and settings are kept.",
            update.version, update.current_version
        );
        let install = app
            .dialog()
            .message(msg)
            .title("Update available")
            .buttons(MessageDialogButtons::OkCancelCustom("Install".into(), "Later".into()))
            .blocking_show();
        if !install {
            return;
        }
        let manual = "Update failed - you can download the new version at\n\
                      cs2analysis.com/demoquery/download";
        // download while the app is still usable...
        let bytes = match tauri::async_runtime::block_on(update.download(|_, _| {}, || {})) {
            Ok(b) => b,
            Err(_) => {
                app.dialog().message(manual).title("Update failed").blocking_show();
                return;
            }
        };
        // ...then stop the sidecar so the installer can overwrite server files
        if let Some(mut child) = app.state::<ServerProc>().0.lock().unwrap().take() {
            kill_tree(&mut child);
        }
        if update.install(bytes).is_err() {
            app.dialog().message(manual).title("Update failed").blocking_show();
            app.exit(1);
            return;
        }
        app.exit(0); // the NSIS takes over and relaunches the app
    });
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .manage(ServerProc(Mutex::new(None)))
        .setup(|app| {
            let port = free_port();
            let exe = app
                .path()
                .resource_dir()?
                .join("server")
                .join("cs2sf-server.exe");
            let child = spawn_server(&exe, port).map_err(|e| {
                format!("failed to start engine at {}: {e}", exe.display())
            })?;
            app.state::<ServerProc>().0.lock().unwrap().replace(child);

            offer_update(app.handle().clone());

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for _ in 0..240 {
                    if health_ok(port) {
                        if let Some(win) = handle.get_webview_window("main") {
                            if let Ok(url) = format!("http://127.0.0.1:{port}/").parse() {
                                let _ = win.navigate(url);
                            }
                        }
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(250));
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building DemoQueryCS2")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut child) = app.state::<ServerProc>().0.lock().unwrap().take() {
                    kill_tree(&mut child);
                }
            }
        });
}
