# Nanobot Desktop E2E Checklist

## Installation
- [ ] MSI installs successfully.
- [ ] Start Menu entry launches `nanobot-desktop`.
- [ ] Only one visible app entry is shown to user.

## Startup
- [ ] Main window opens.
- [ ] Desktop connection status reaches `connected`.
- [ ] Sidecar process is running.
- [ ] `app.health` returns `ok=true`.

## Core Features
- [ ] Chat page sends a message and receives response.
- [ ] Sessions page lists sessions and opens one session.
- [ ] Integrations page shows channel states.
- [ ] Tasks page can refresh codex/antigravity status.
- [ ] Settings page can save a config patch.
- [ ] Logs page can export diagnostics archive.

## Security and Permissions
- [ ] WebSocket without token is rejected.
- [ ] Token-based connection succeeds.
- [ ] Default permission policy remains safe-mode unless explicitly changed.

## Stability
- [ ] Close/reopen app without orphan processes.
- [ ] Restart sidecar from tray menu works.
- [ ] 24h run test has no crash in normal usage.
