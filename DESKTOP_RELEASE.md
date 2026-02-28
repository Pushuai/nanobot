# Nanobot Desktop Release Guide

## Prerequisites
- Windows 10/11 x64
- Rust toolchain (stable, `cargo`, `rustc`)
- Node.js 18+
- Python 3.11+ (same env as nanobot runtime)
- WebView2 runtime (normally preinstalled on modern Windows)

## Build Steps
1. Build backend sidecar:
   - `powershell -ExecutionPolicy Bypass -File .\scripts\build_backend_sidecar.ps1`
2. Build desktop frontend and MSI:
   - `powershell -ExecutionPolicy Bypass -File .\scripts\build_desktop_app.ps1 -SkipSidecar`

## Output Locations
- Sidecar exe:
  - `desktop-app/resources/backend/nanobot-desktop-backend.exe`
- Desktop bundle:
  - `desktop-app/src-tauri/target/release/bundle/msi/*.msi`

## Runtime Expectations
- Main process: `nanobot-desktop.exe`
- Sidecar process (hidden): `nanobot-desktop-backend.exe` or `python -m nanobot desktop-gateway` (dev mode)
- Desktop API:
  - `ws://127.0.0.1:18791/ws`
- Auth token file:
  - `~/.nanobot/desktop/token.json`

## Upgrade and Rollback
- Upgrade:
  - install newer MSI over existing version
- Rollback:
  - uninstall current MSI, install previous MSI
- User data remains in `~/.nanobot` unless explicitly removed.

## Auto Start
- Register current-user startup task:
  - `powershell -ExecutionPolicy Bypass -File .\scripts\register_desktop_startup.ps1`
- Remove startup task:
  - `powershell -ExecutionPolicy Bypass -File .\scripts\unregister_desktop_startup.ps1`

## Ops Runbook
- See `DESKTOP_RUNBOOK.md` for install, startup, verification, upgrade, and rollback SOP.
