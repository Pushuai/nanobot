# Nanobot Desktop Runbook (Windows)

## 1. Build and Install
1. Build sidecar:
   - `powershell -ExecutionPolicy Bypass -File .\scripts\build_backend_sidecar.ps1`
2. Build desktop app and MSI:
   - `powershell -ExecutionPolicy Bypass -File .\scripts\build_desktop_app.ps1 -SkipSidecar`
3. Install MSI:
   - `desktop-app\src-tauri\target\release\bundle\msi\Nanobot Desktop_0.1.0_x64_en-US.msi`

## 2. First Launch Validation
1. Start `nanobot-desktop.exe`.
2. Confirm desktop can connect to sidecar (status is connected).
3. Send a message on Chat page and confirm response.
4. Open Logs page and run diagnostics export once.

## 3. Enable Auto Start (Current User)
Use scheduled task to start desktop at logon:

- Register:
  - `powershell -ExecutionPolicy Bypass -File .\scripts\register_desktop_startup.ps1`
- Register with explicit path:
  - `powershell -ExecutionPolicy Bypass -File .\scripts\register_desktop_startup.ps1 -ExePath "C:\Path\To\nanobot-desktop.exe"`
- Remove:
  - `powershell -ExecutionPolicy Bypass -File .\scripts\unregister_desktop_startup.ps1`

## 4. Upgrade
1. Build a new MSI.
2. Install the new MSI over current version.
3. Launch and verify connectivity and chat once.

## 5. Rollback
1. Uninstall current MSI from Windows Apps.
2. Install previous MSI.
3. Re-test chat and diagnostics export.

## 6. Troubleshooting
- Sidecar auth token:
  - `%USERPROFILE%\.nanobot\desktop\token.json`
- Desktop runtime lock:
  - `%USERPROFILE%\.nanobot\desktop\runtime.lock`
- Diagnostics zip output:
  - `%USERPROFILE%\.nanobot\desktop\diagnostics\`
- If desktop cannot connect:
  - run `python -m nanobot desktop-gateway --host 127.0.0.1 --port 18791`
  - verify local WS endpoint `ws://127.0.0.1:18791/ws`
