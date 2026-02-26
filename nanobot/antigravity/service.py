"""Antigravity CLI service built on top of Codex integration logic."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.codex.service import CodexService
from nanobot.config.schema import AntigravityConfig
from nanobot.utils.helpers import get_data_path
from nanobot.utils.protobuf_raw_decode import extract_printable_strings


class AntigravityService(CodexService):
    """Antigravity integration in listener-only mode."""

    _AG_CONVERSATION_POLL_INTERVAL_SECONDS = 2.0
    _AG_CONVERSATION_FAST_NOTIFY_SECONDS = 2.0
    _AG_BRAIN_RECENT_WINDOW_SECONDS = 180.0
    _AG_SESSION_NOTIFY_COOLDOWN_SECONDS = 60.0
    _AG_COMPLETION_KEYWORDS = (
        "all items completed",
        "task completed",
        "completed successfully",
        "all done",
        "done",
        "finished",
        "complete",
    )

    def __init__(
        self,
        config: AntigravityConfig,
        bus: MessageBus,
        default_work_dir: str | None = None,
    ):
        super().__init__(config=config, bus=bus, default_work_dir=default_work_dir)
        self._ag_conversations_bootstrapped = False
        self._ag_last_conversation_poll_ts = 0.0
        self._ag_conversation_seen_mtime: dict[str, float] = {}
        self._ag_conversation_pending: dict[str, tuple[float, float, Path, str]] = {}
        self._ag_conversation_notified: set[str] = set()
        self._ag_session_last_notify_ts: dict[str, float] = {}

    def _service_slug(self) -> str:
        return "antigravity"

    def _service_display_name(self) -> str:
        return "Antigravity"

    def _command_prefix(self) -> str:
        prefix = (self.config.command_prefix or "").strip()
        return prefix or "/ag"

    def _sessions_root(self) -> Path:
        """
        Resolve Antigravity rollout directory.

        Keep this strictly scoped to Antigravity so Codex rollout files
        are not interpreted as Antigravity completion events.
        """
        return Path.home() / ".antigravity" / "sessions"

    def _conversations_root(self) -> Path:
        """
        Resolve Antigravity local conversation snapshots.

        Current desktop builds persist conversation activity under:
        ~/.gemini/antigravity/conversations/*.pb
        """
        return Path.home() / ".gemini" / "antigravity" / "conversations"

    def _brain_root(self) -> Path:
        return Path.home() / ".gemini" / "antigravity" / "brain"

    @staticmethod
    def _read_notify_route_file(path: Path) -> tuple[str, str] | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        channel = (raw.get("channel") or "").strip()
        chat_id = (raw.get("chat_id") or "").strip()
        if channel and chat_id:
            return (channel, chat_id)
        return None

    def _get_notify_route(self) -> tuple[str, str] | None:
        # 1) Prefer persisted antigravity route and reload it each time to avoid
        # stale in-memory target after manual file edits or recovery scripts.
        ag_notify_state = get_data_path() / "antigravity_runs" / "notify_route.json"
        if route := self._read_notify_route_file(ag_notify_state):
            self._default_notify_route = route
            return route

        # 2) fallback to in-memory route from current runtime.
        if route := super()._get_notify_route():
            return route

        # 3) fallback to codex bound chat so listener-only antigravity can notify
        # without introducing extra bind commands.
        codex_notify_state = get_data_path() / "codex_runs" / "notify_route.json"
        return self._read_notify_route_file(codex_notify_state)

    def _maybe_notify_external_approval(self, path: Path, obj: dict[str, Any], sid: str) -> None:
        # Listener-only mode intentionally ignores approval reminders.
        return

    def _poll_external_sessions(self) -> None:
        super()._poll_external_sessions()
        self._poll_conversation_activity()

    def _poll_conversation_activity(self) -> None:
        if not self._loop:
            return
        now = time.time()
        if (now - self._ag_last_conversation_poll_ts) < self._AG_CONVERSATION_POLL_INTERVAL_SECONDS:
            return
        self._ag_last_conversation_poll_ts = now

        files = self._iter_conversation_files(limit=200)
        active_ids: set[str] = set()

        for path in files:
            sid = path.stem.strip()
            if not sid:
                continue
            active_ids.add(sid)
            try:
                mtime = path.stat().st_mtime
            except Exception:
                continue

            prev = self._ag_conversation_seen_mtime.get(sid)
            self._ag_conversation_seen_mtime[sid] = mtime
            if not self._ag_conversations_bootstrapped:
                continue

            if prev is None or mtime > (prev + 1e-6):
                reason = self._detect_completion_hint(sid=sid, conv_mtime=mtime)
                if reason:
                    deadline = now + self._AG_CONVERSATION_FAST_NOTIFY_SECONDS
                    self._ag_conversation_pending[sid] = (mtime, deadline, path, reason)
                else:
                    self._ag_conversation_pending.pop(sid, None)

        if not self._ag_conversations_bootstrapped:
            self._ag_conversations_bootstrapped = True
            return

        for sid in list(self._ag_conversation_pending):
            if sid not in active_ids:
                self._ag_conversation_pending.pop(sid, None)

        for sid, (mtime, deadline, path, reason) in list(self._ag_conversation_pending.items()):
            if now < deadline:
                continue
            event_key = f"{sid}:{mtime:.6f}"
            if event_key in self._ag_conversation_notified:
                self._ag_conversation_pending.pop(sid, None)
                continue
            last_ts = self._ag_session_last_notify_ts.get(sid, 0.0)
            if (now - last_ts) < self._AG_SESSION_NOTIFY_COOLDOWN_SECONDS:
                self._ag_conversation_pending.pop(sid, None)
                continue

            route = self._get_notify_route()
            if not route:
                continue
            channel, chat_id = route
            updated_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            session_content = self._build_conversation_content(
                sid=sid,
                conv_mtime=mtime,
                conversation_path=path,
            )
            content = (
                "### Antigravity external session complete\n\n"
                f"**Session ID**: `{sid}`\n"
                f"**Updated At**: `{updated_at}`\n"
                f"**Source**: `~/.gemini/antigravity/conversations/{path.name}`\n\n"
                f"**Detect Mode**: `{reason}`"
            )
            if session_content:
                content += f"\n\n**Session Content**\n{session_content}"
            self._notify_route(channel, chat_id, content, fmt="markdown")
            self._ag_conversation_notified.add(event_key)
            self._ag_session_last_notify_ts[sid] = now
            self._ag_conversation_pending.pop(sid, None)

        if len(self._ag_conversation_notified) > 4000:
            self._ag_conversation_notified.clear()
        if len(self._ag_session_last_notify_ts) > 1000:
            cutoff = now - (self._AG_SESSION_NOTIFY_COOLDOWN_SECONDS * 10)
            self._ag_session_last_notify_ts = {
                sid: ts
                for sid, ts in self._ag_session_last_notify_ts.items()
                if ts >= cutoff
            }

    def _iter_conversation_files(self, limit: int = 200) -> list[Path]:
        root = self._conversations_root()
        if not root.exists():
            return []
        files = list(root.glob("*.pb"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]

    @staticmethod
    def _normalize_preview_text(text: str, max_chars: int = 320) -> str:
        normalized = re.sub(r"\s+", " ", (text or "")).strip()
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max_chars - 3].rstrip() + "..."

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except Exception:
            return None

    def _build_conversation_content(
        self,
        sid: str,
        conv_mtime: float,
        conversation_path: Path | None = None,
    ) -> str | None:
        sections: list[str] = []
        summaries = self._collect_brain_summaries(sid=sid, conv_mtime=conv_mtime)
        if summaries:
            sections.append("Summary:\n" + "\n".join(summaries))

        completed = self._collect_completed_items(sid=sid, conv_mtime=conv_mtime)
        if completed:
            sections.append("Completed Items:\n" + "\n".join(completed))

        if not sections and conversation_path is not None:
            raw_preview = self._collect_pb_preview(conversation_path)
            if raw_preview:
                sections.append("Raw Preview:\n" + "\n".join(raw_preview))

        if not sections:
            return None
        return "\n\n".join(sections)

    def _collect_brain_summaries(self, sid: str, conv_mtime: float) -> list[str]:
        brain_dir = self._brain_root() / sid
        if not brain_dir.exists():
            return []

        candidates = (
            ("walkthrough.md.metadata.json", "walkthrough"),
            ("task.md.metadata.json", "task"),
            ("implementation_plan.md.metadata.json", "plan"),
        )
        recent: list[str] = []
        fallback: list[str] = []
        for filename, label in candidates:
            path = brain_dir / filename
            if not path.exists():
                continue
            raw = self._read_json_file(path)
            if not isinstance(raw, dict):
                continue
            summary = self._normalize_preview_text(str(raw.get("summary") or ""), max_chars=280)
            if not summary:
                continue
            line = f"- {label}: {summary}"
            try:
                mtime = path.stat().st_mtime
            except Exception:
                mtime = 0.0
            if abs(mtime - conv_mtime) <= self._AG_BRAIN_RECENT_WINDOW_SECONDS:
                recent.append(line)
            else:
                fallback.append(line)

        source = recent or fallback
        deduped: list[str] = []
        seen: set[str] = set()
        for item in source:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
            if len(deduped) >= 3:
                break
        return deduped

    def _collect_completed_items(self, sid: str, conv_mtime: float) -> list[str]:
        task_path = self._brain_root() / sid / "task.md"
        if not task_path.exists():
            return []
        try:
            mtime = task_path.stat().st_mtime
            if abs(mtime - conv_mtime) > (self._AG_BRAIN_RECENT_WINDOW_SECONDS * 2):
                return []
            text = task_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []

        checked = re.findall(r"^\s*-\s*\[x\]\s*(.+)$", text, flags=re.I | re.M)
        out: list[str] = []
        for item in checked[:5]:
            normalized = self._normalize_preview_text(item, max_chars=200)
            if normalized:
                out.append(f"- {normalized}")
        return out

    def _collect_pb_preview(self, conversation_path: Path) -> list[str]:
        try:
            data = conversation_path.read_bytes()
        except Exception:
            return []

        raw_items = extract_printable_strings(data, min_len=16, limit=20)
        out: list[str] = []
        for item in raw_items:
            normalized = self._normalize_preview_text(item, max_chars=220)
            if len(normalized) < 24:
                continue
            out.append(f"- {normalized}")
            if len(out) >= 3:
                break
        return out

    def _detect_completion_hint(self, sid: str, conv_mtime: float) -> str | None:
        """
        Try to detect completion using Antigravity brain artifacts.

        We only trust hints when the brain files are updated near the current
        conversation mtime to avoid stale historical summaries.
        """
        brain_dir = self._brain_root() / sid
        if not brain_dir.exists():
            return None

        markers: list[str] = []
        recent = False

        meta_path = brain_dir / "task.md.metadata.json"
        if meta_path.exists():
            try:
                meta_mtime = meta_path.stat().st_mtime
                if abs(meta_mtime - conv_mtime) <= self._AG_BRAIN_RECENT_WINDOW_SECONDS:
                    recent = True
                raw = self._read_json_file(meta_path) or {}
                summary = str(raw.get("summary") or "").strip().lower()
                if summary and any(k in summary for k in self._AG_COMPLETION_KEYWORDS):
                    markers.append("meta_summary")
            except Exception:
                pass

        task_path = brain_dir / "task.md"
        if task_path.exists():
            try:
                task_mtime = task_path.stat().st_mtime
                if abs(task_mtime - conv_mtime) <= self._AG_BRAIN_RECENT_WINDOW_SECONDS:
                    recent = True
                text = task_path.read_text(encoding="utf-8", errors="ignore")
                checked = len(re.findall(r"^\s*-\s*\[x\]", text, flags=re.I | re.M))
                unchecked = len(re.findall(r"^\s*-\s*\[\s\]", text, flags=re.M))
                if checked > 0 and unchecked == 0:
                    markers.append("task_all_checked")
                lower = text.lower()
                if any(k in lower for k in self._AG_COMPLETION_KEYWORDS):
                    markers.append("task_text")
            except Exception:
                pass

        if not markers or not recent:
            return None

        uniq: list[str] = []
        for marker in markers:
            if marker not in uniq:
                uniq.append(marker)
        return "brain_" + "_".join(uniq)

    def _handle_external_line(self, path: Path, raw: str) -> None:
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return

        sid = self._extract_session_id(obj) or self._extract_session_id_from_path(path)
        if obj.get("type") == "session_meta":
            payload = obj.get("payload") or {}
            if payload.get("id"):
                self._external_meta[payload["id"]] = payload
            return

        event_type = obj.get("type")
        payload: dict[str, Any]
        if event_type == "event_msg":
            payload = obj.get("payload") or {}
            if payload.get("type") != "task_complete":
                return
        elif event_type == "task_complete":
            # Compatibility: some clients may emit task_complete directly.
            payload = obj
        else:
            return

        if not sid:
            return

        meta = self._find_external_meta(sid)
        if not meta:
            if file_meta := self._read_session_meta(path):
                meta = file_meta
                if file_meta.get("id"):
                    self._external_meta[file_meta["id"]] = file_meta
        cwd = (meta or {}).get("cwd")
        if self._is_owned_or_recent_local(sid, cwd):
            logger.info(
                f"Skip external duplicate {self._service_slug()} turn: session={sid}, cwd={cwd or 'N/A'}"
            )
            return

        event_key = f"{sid}:{payload.get('turn_id') or obj.get('timestamp') or ''}"
        if event_key in self._external_seen:
            return
        self._external_seen.add(event_key)

        route = self._get_notify_route()
        if not route:
            return
        channel, chat_id = route
        workspace = cwd or "N/A"
        last_msg = (payload.get("last_agent_message") or "").strip()
        session_content = self._build_conversation_content(
            sid=sid,
            conv_mtime=time.time(),
            conversation_path=None,
        )

        if last_msg:
            content = (
                f"{last_msg}\n\n"
                f"---\n"
                "### Antigravity external session complete\n"
                f"**Workspace**: `{workspace}`\n"
                f"**Session ID**: `{sid}`"
            )
        else:
            content = (
                "### Antigravity external session complete\n\n"
                f"**Workspace**: `{workspace}`\n"
                f"**Session ID**: `{sid}`"
            )
            if session_content:
                content += f"\n\n**Session Content**\n{session_content}"

        self._notify_route(channel, chat_id, content, fmt="markdown")
