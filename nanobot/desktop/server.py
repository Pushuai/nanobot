"""Desktop WebSocket API server."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from loguru import logger

from nanobot import __version__
from nanobot.channels.desktop import DesktopChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.loader import convert_keys, convert_to_camel, save_config
from nanobot.config.schema import Config, DesktopConfig
from nanobot.desktop.protocol import API_VERSION, make_event, make_response, parse_request
from nanobot.utils.helpers import get_data_path
from nanobot.session.manager import SessionManager


class DesktopServer:
    """WebSocket bridge between desktop UI and nanobot runtime."""

    def __init__(
        self,
        desktop_config: DesktopConfig,
        config: Config,
        channel_manager: ChannelManager,
        session_manager: SessionManager,
        desktop_channel: DesktopChannel,
        auth_token: str,
        codex_service: Any = None,
        antigravity_service: Any = None,
    ):
        self.desktop_config = desktop_config
        self.config = config
        self.channel_manager = channel_manager
        self.session_manager = session_manager
        self.desktop_channel = desktop_channel
        self.auth_token = auth_token
        self.codex = codex_service
        self.antigravity = antigravity_service
        self._server: Any = None
        self._clients: set[Any] = set()

    async def start(self) -> None:
        self.desktop_channel.set_outbound_handler(self._on_outbound_message)
        self._server = await websockets.serve(
            self._on_client,
            self.desktop_config.host,
            int(self.desktop_config.port),
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info(
            "Desktop WS server started at ws://{}:{}/ws",
            self.desktop_config.host,
            self.desktop_config.port,
        )

    async def stop(self) -> None:
        self.desktop_channel.set_outbound_handler(None)
        # Close active clients first.
        clients = list(self._clients)
        self._clients.clear()
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.info("Desktop WS server stopped")

    async def _on_client(self, websocket: Any, path: str | None = None) -> None:
        path = self._resolve_request_path(websocket, path)
        if not self._is_authorized(websocket, path):
            await websocket.close(code=4401, reason="unauthorized")
            return

        self._clients.add(websocket)
        await websocket.send(
            json.dumps(
                make_event(
                    "system.notice",
                    {
                        "message": "desktop connected",
                        "ts": datetime.now().isoformat(),
                    },
                ),
                ensure_ascii=False,
            )
        )
        try:
            async for raw in websocket:
                response = await self._handle_message(raw)
                await websocket.send(json.dumps(response, ensure_ascii=False))
        except websockets.ConnectionClosed:
            return
        except Exception as e:
            logger.error(f"Desktop WS client error: {e}")
        finally:
            self._clients.discard(websocket)

    async def _handle_message(self, raw: str) -> dict[str, Any]:
        req_id = None
        try:
            obj = json.loads(raw)
            req_id, action, payload = parse_request(obj)
            data = await self._dispatch_action(action, payload)
            return make_response(req_id, ok=True, data=data)
        except Exception as e:
            return make_response(req_id, ok=False, error=str(e))

    async def _dispatch_action(self, action: str, payload: dict[str, Any]) -> Any:
        if action == "app.ping":
            return {"pong": True, "ts": datetime.now().isoformat()}
        if action == "app.version":
            return {"apiVersion": API_VERSION, "nanobotVersion": __version__}
        if action == "app.health":
            return self._health()
        if action == "config.get":
            return self._config_get()
        if action == "config.patch":
            return self._config_patch(payload)
        if action == "session.list":
            return {"sessions": self.session_manager.list_sessions()}
        if action == "session.open":
            return self._session_open(payload)
        if action == "chat.send":
            return await self._chat_send(payload)
        if action == "chat.stop":
            return {"supported": False, "message": "chat.stop is not supported"}
        if action == "channel.status":
            return {"channels": self.channel_manager.get_status()}
        if action == "channel.toggle":
            return self._channel_toggle(payload)
        if action == "codex.list":
            return self._codex_list()
        if action == "codex.run":
            return self._codex_run(payload)
        if action == "codex.resume":
            return self._codex_resume(payload)
        if action == "codex.stop":
            return self._codex_stop(payload)
        if action == "antigravity.status":
            return self._antigravity_status()
        if action == "diagnostics.paths":
            return self._diagnostics_paths()
        if action == "diagnostics.export":
            return self._diagnostics_export(payload)
        raise ValueError(f"unsupported action: {action}")

    async def _on_outbound_message(self, msg: Any) -> None:
        source = (msg.metadata or {}).get("source", "")
        event_name = "task.updated" if source in ("codex", "antigravity") else "chat.delta"
        event = make_event(
            event_name,
            {
                "channel": msg.channel,
                "chatId": msg.chat_id,
                "content": msg.content,
                "metadata": msg.metadata or {},
            },
        )
        await self._broadcast(event)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        closed: list[Any] = []
        for ws in list(self._clients):
            try:
                await ws.send(text)
            except Exception:
                closed.append(ws)
        for ws in closed:
            self._clients.discard(ws)

    def _health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "apiVersion": API_VERSION,
            "nanobotVersion": __version__,
            "clients": len(self._clients),
            "channels": self.channel_manager.get_status(),
            "codexEnabled": bool(self.codex and self.codex.config.enabled),
            "antigravityEnabled": bool(self.antigravity and self.antigravity.config.enabled),
        }

    def _config_get(self) -> dict[str, Any]:
        data = self.config.model_dump()
        providers = data.get("providers", {})
        if isinstance(providers, dict):
            for _, item in providers.items():
                if isinstance(item, dict) and item.get("api_key"):
                    item["api_key"] = "***"
        return convert_to_camel(data)

    def _config_patch(self, payload: dict[str, Any]) -> dict[str, Any]:
        patch = payload.get("patch", payload)
        if not isinstance(patch, dict) or not patch:
            raise ValueError("patch is required")
        patch_snake = convert_keys(patch)
        current = self.config.model_dump()
        merged = self._deep_merge(current, patch_snake)
        next_config = Config.model_validate(merged)
        save_config(next_config)
        self.config = next_config
        self.desktop_config = next_config.desktop
        return {"saved": True, "restartRequired": True}

    def _session_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("sessionKey") or payload.get("key") or "").strip()
        if not key:
            raise ValueError("sessionKey is required")
        limit = int(payload.get("limit") or 200)
        limit = max(1, min(limit, 1000))
        session = self.session_manager.get_or_create(key)
        return {
            "sessionKey": key,
            "messages": session.messages[-limit:],
            "updatedAt": session.updated_at.isoformat(),
        }

    async def _chat_send(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        chat_id = str(
            payload.get("chatId")
            or self.config.channels.desktop.default_chat_id
            or "desktop-ui"
        ).strip() or "desktop-ui"
        sender_id = str(payload.get("senderId") or "desktop-user")
        await self.desktop_channel.publish_from_ui(
            content=content,
            chat_id=chat_id,
            sender_id=sender_id,
            metadata={"source": "desktop_ui"},
        )
        return {"accepted": True, "chatId": chat_id}

    def _channel_toggle(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("channel") or "").strip().lower()
        if not name:
            raise ValueError("channel is required")
        if name == "desktop":
            raise ValueError("desktop channel cannot be disabled at runtime")
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true/false")
        current = self.config.model_dump()
        channels = current.get("channels") or {}
        target = channels.get(name)
        if not isinstance(target, dict):
            raise ValueError(f"unknown channel: {name}")
        target["enabled"] = enabled
        next_config = Config.model_validate(current)
        save_config(next_config)
        self.config = next_config
        return {"saved": True, "restartRequired": True, "channel": name, "enabled": enabled}

    def _codex_list(self) -> dict[str, Any]:
        if not self.codex:
            return {"enabled": False, "text": "Codex integration is not enabled."}
        return {"enabled": True, "text": self.codex.list_runs_text()}

    def _codex_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.codex:
            raise ValueError("codex integration is not enabled")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        name = (payload.get("name") or None)
        chat_id = str(payload.get("chatId") or self.config.channels.desktop.default_chat_id or "desktop-ui")
        extra_args = payload.get("extraArgs") or []
        if not isinstance(extra_args, list):
            raise ValueError("extraArgs must be an array")
        run = self.codex.run_exec(
            prompt=prompt,
            name=name,
            channel="desktop",
            chat_id=chat_id,
            work_dir=payload.get("workDir"),
            extra_args=extra_args,
        )
        return {"runId": run.run_id, "name": run.name, "workspace": run.cwd}

    def _codex_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.codex:
            raise ValueError("codex integration is not enabled")
        session_id = str(payload.get("sessionId") or "").strip() or None
        prompt = str(payload.get("prompt") or "").strip()
        name = payload.get("name") or None
        chat_id = str(payload.get("chatId") or self.config.channels.desktop.default_chat_id or "desktop-ui")
        extra_args = payload.get("extraArgs") or []
        if not isinstance(extra_args, list):
            raise ValueError("extraArgs must be an array")
        run = self.codex.run_resume(
            session_id=session_id,
            prompt=prompt,
            name=name,
            channel="desktop",
            chat_id=chat_id,
            extra_args=extra_args,
        )
        return {"runId": run.run_id, "name": run.name, "sessionId": run.session_id}

    def _codex_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.codex:
            raise ValueError("codex integration is not enabled")
        target = str(payload.get("nameOrId") or payload.get("name") or payload.get("runId") or "").strip()
        if not target:
            raise ValueError("nameOrId is required")
        text = self.codex.stop_run(target)
        return {"text": text}

    def _antigravity_status(self) -> dict[str, Any]:
        if not self.antigravity:
            return {"enabled": False, "text": "Antigravity integration is not enabled."}
        return {"enabled": True, "text": self.antigravity.list_runs_text()}

    def _diagnostics_paths(self) -> dict[str, Any]:
        root = get_data_path()
        return {
            "root": str(root),
            "files": [
                str(root / "config.json"),
                str(root / "gateway.out.log"),
                str(root / "gateway.err.log"),
                str(root / "bus_inbound.log"),
                str(root / "bus_outbound.log"),
                str(root / "agent_inbound.log"),
                str(root / "feishu_inbound.log"),
                str(root / "feishu_outbound.log"),
                str(root / "desktop" / "runtime.lock"),
                str(root / "desktop" / "token.json"),
            ],
        }

    def _diagnostics_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = get_data_path()
        out_path_str = str(payload.get("outputPath") or "").strip()
        if out_path_str:
            out_path = Path(out_path_str).expanduser()
            if out_path.is_dir():
                out_file = out_path / self._diagnostic_filename()
            else:
                out_file = out_path
        else:
            out_dir = root / "desktop" / "diagnostics"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / self._diagnostic_filename()

        include = self._collect_diagnostic_files(root)
        added: list[Path] = []
        skipped: list[dict[str, str]] = []
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in include:
                try:
                    arc = p.relative_to(root)
                except Exception:
                    arc = p.name
                try:
                    zf.write(p, arcname=str(arc))
                    added.append(p)
                except Exception as e:
                    skipped.append({"path": str(p), "error": str(e)})
        return {
            "archive": str(out_file),
            "count": len(added),
            "skipped": skipped,
        }

    @staticmethod
    def _diagnostic_filename() -> str:
        return datetime.now().strftime("nanobot-diagnostics-%Y%m%d-%H%M%S.zip")

    @staticmethod
    def _collect_diagnostic_files(root: Path) -> list[Path]:
        candidates = [
            root / "config.json",
            root / "gateway.out.log",
            root / "gateway.err.log",
            root / "bus_inbound.log",
            root / "bus_outbound.log",
            root / "agent_inbound.log",
            root / "feishu_inbound.log",
            root / "feishu_outbound.log",
            root / "desktop" / "token.json",
            root / "desktop" / "runtime.lock",
        ]
        files: list[Path] = []
        for p in candidates:
            if p.exists() and p.is_file():
                files.append(p)
        return files

    def _is_authorized(self, websocket: Any, path: str) -> bool:
        expected = (self.auth_token or "").strip()
        if not expected:
            return True
        # Query token: ws://.../ws?token=...
        query = parse_qs(urlsplit(path).query)
        query_token = (query.get("token") or [""])[0].strip()
        if query_token == expected:
            return True
        # Header token: Authorization: Bearer xxx or X-Nanobot-Token: xxx
        headers = self._resolve_request_headers(websocket)
        auth = str(headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer ") and auth[7:].strip() == expected:
            return True
        custom = str(headers.get("X-Nanobot-Token") or "").strip()
        return custom == expected

    @staticmethod
    def _resolve_request_path(websocket: Any, fallback: str | None = None) -> str:
        if fallback:
            return fallback
        req = getattr(websocket, "request", None)
        req_path = getattr(req, "path", None)
        if isinstance(req_path, str) and req_path:
            return req_path
        path = getattr(websocket, "path", None)
        if isinstance(path, str) and path:
            return path
        return "/"

    @staticmethod
    def _resolve_request_headers(websocket: Any) -> dict[str, Any]:
        raw = getattr(websocket, "request_headers", None)
        if raw is None:
            req = getattr(websocket, "request", None)
            raw = getattr(req, "headers", None)
        if raw is None:
            return {}
        try:
            # websocket headers object already supports `.get()`, but converting to
            # plain dict normalizes behavior across websocket versions.
            return dict(raw)
        except Exception:
            return raw if hasattr(raw, "get") else {}

    @staticmethod
    def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k] = DesktopServer._deep_merge(base[k], v)
            else:
                base[k] = v
        return base
