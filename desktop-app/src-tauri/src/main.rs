#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde_json::Value;
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, State};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

struct SidecarState(Mutex<Option<Child>>);

#[tauri::command]
fn desktop_token() -> Result<String, String> {
    let home = dirs::home_dir().ok_or("home directory not found")?;
    let token_path = home.join(".nanobot").join("desktop").join("token.json");
    let text = std::fs::read_to_string(&token_path)
        .map_err(|e| format!("failed to read token file {}: {e}", token_path.display()))?;
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("invalid token json: {e}"))?;
    Ok(v.get("token")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string())
}

#[tauri::command]
fn sidecar_status(state: State<'_, SidecarState>) -> bool {
    let mut guard = match state.0.lock() {
        Ok(g) => g,
        Err(_) => return false,
    };
    if let Some(child) = guard.as_mut() {
        return matches!(child.try_wait(), Ok(None));
    }
    false
}

#[tauri::command]
fn restart_sidecar(app: AppHandle, state: State<'_, SidecarState>) -> Result<(), String> {
    stop_sidecar(&state);
    ensure_sidecar_running(&app, &state)
}

fn ensure_sidecar_running(app: &AppHandle, state: &State<'_, SidecarState>) -> Result<(), String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "sidecar mutex poisoned".to_string())?;
    if let Some(child) = guard.as_mut() {
        match child.try_wait() {
            Ok(None) => return Ok(()),
            Ok(Some(_)) => {
                *guard = None;
            }
            Err(e) => return Err(format!("failed to inspect sidecar process: {e}")),
        }
    }

    let mut last_error = String::from("no sidecar command candidates");
    for (program, args, cwd) in sidecar_command_candidates(app) {
        let mut cmd = Command::new(&program);
        cmd.args(args);
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::null());
        cmd.stderr(Stdio::null());
        if let Some(workdir) = cwd {
            cmd.current_dir(workdir);
        }
        #[cfg(target_os = "windows")]
        {
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        match cmd.spawn() {
            Ok(child) => {
                *guard = Some(child);
                return Ok(());
            }
            Err(e) => {
                last_error = format!("spawn {program} failed: {e}");
            }
        }
    }

    Err(last_error)
}

fn stop_sidecar(state: &State<'_, SidecarState>) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn sidecar_command_candidates(app: &AppHandle) -> Vec<(String, Vec<String>, Option<PathBuf>)> {
    let mut out = Vec::new();

    // Packaged private backend
    if let Ok(resource_dir) = app.path().resource_dir() {
        let exe = resource_dir
            .join("backend")
            .join(if cfg!(target_os = "windows") {
                "nanobot-desktop-backend.exe"
            } else {
                "nanobot-desktop-backend"
            });
        if exe.exists() {
            out.push((
                exe.to_string_lossy().into_owned(),
                Vec::new(),
                Some(resource_dir),
            ));
        }
    }

    // Development fallback: run local Python module
    if let Ok(project_dir) = std::env::current_dir() {
        out.push((
            "python".to_string(),
            vec![
                "-m".to_string(),
                "nanobot".to_string(),
                "desktop-gateway".to_string(),
            ],
            Some(project_dir),
        ));
    }
    out
}

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let show_item = MenuItemBuilder::with_id("show", "Show").build(app)?;
    let restart_item = MenuItemBuilder::with_id("restart_sidecar", "Restart Sidecar").build(app)?;
    let quit_item = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
    let menu = MenuBuilder::new(app)
        .items(&[&show_item, &restart_item, &quit_item])
        .build()?;

    let app_handle = app.clone();
    TrayIconBuilder::new()
        .menu(&menu)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "show" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "restart_sidecar" => {
                let state = app.state::<SidecarState>();
                let _ = restart_sidecar(app.clone(), state);
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(move |_tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(window) = app_handle.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
        })
        .build(app)?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .manage(SidecarState(Mutex::new(None)))
        .setup(|app| {
            setup_tray(app.handle())?;
            let state = app.state::<SidecarState>();
            if let Err(err) = ensure_sidecar_running(app.handle(), &state) {
                eprintln!("Failed to start sidecar: {err}");
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_token,
            sidecar_status,
            restart_sidecar
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
