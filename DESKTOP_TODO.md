# Nanobot Desktop Delivery TODO

## 1) Backend Runtime
- [x] Add desktop config models (`desktop`, `channels.desktop`)
- [x] Add desktop virtual channel and manager integration
- [x] Add desktop WebSocket API server with auth token and runtime lock
- [x] Add `nanobot desktop-gateway` CLI command
- [x] Add diagnostics endpoints (`diagnostics.paths`, `diagnostics.export`)
- [x] Fix websocket auth compatibility for new `websockets` server API (`request.path/headers`)
- [x] Fix diagnostics export on Windows lock files (skip locked files, return `skipped`)

## 2) Desktop UI (Tauri + React)
- [x] Scaffold `desktop-app` project
- [x] Implement desktop WS client SDK + shared desktop context
- [x] Implement pages: Chat / Sessions / Integrations / Tasks / Settings / Logs
- [x] Implement sidecar lifecycle (spawn, status, restart, reconnect)
- [x] Implement single-instance and tray menu

## 3) Packaging and Release
- [x] Add sidecar build entry (`scripts/desktop_sidecar_entry.py`)
- [x] Add sidecar build script (`scripts/build_backend_sidecar.ps1`)
- [x] Add desktop build script (`scripts/build_desktop_app.ps1`)
- [x] Add strict artifact checks in build scripts (backend exe + MSI existence)
- [x] Add Tauri icon resources and bundling config (`icons/icon.ico`, `bundle.icon`)
- [x] Add release and verification docs (`DESKTOP_RELEASE.md`, `DESKTOP_E2E_CHECKLIST.md`)

## 4) Validation (Executed)
- [x] Python compile check passed for desktop-related backend modules
- [x] Frontend build passed (`npm run build`)
- [x] Tauri rust compile passed (`cargo check`)
- [x] Desktop production bundle passed (`npm run tauri:build`, MSI generated)
- [x] Backend sidecar build passed (`PyInstaller`, onefile exe generated)
- [x] Desktop gateway smoke test passed (`app.ping/config.get/channel.status/chat.send/diagnostics.*`)
- [x] CLI help/status checks passed (`python -m nanobot desktop-gateway --help`, `channels status`)
