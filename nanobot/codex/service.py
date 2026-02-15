"""Codex CLI service for running and monitoring Codex tasks."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import shutil

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import CodexConfig
from nanobot.utils.helpers import get_data_path


@dataclass
class CodexRun:
    run_id: str
    name: str
    cmd: list[str]
    cwd: str
    log_path: Path
    status: str = "running"  # running | finished | stopped | failed
    pid: int | None = None
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    finished_at: str | None = None
    exit_code: int | None = None
    channel: str = ""
    chat_id: str = ""
    stream_pending: str = ""
    last_stream_ts: float = 0.0
    last_answer: str = ""
    last_error_text: str = ""
    is_json: bool = False
    session_id: str | None = None
    awaiting_approval: bool = False
    approval_prompt: str = ""
    approval_requested_at: str | None = None


class CodexService:
    """Manage Codex CLI runs and stream output to chat."""

    def __init__(self, config: CodexConfig, bus: MessageBus, default_work_dir: str | None = None):
        self.config = config
        self.bus = bus
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._runs: dict[str, CodexRun] = {}
        self._owned_session_ids: set[str] = set()
        self._external_offsets: dict[str, int] = {}
        self._external_meta: dict[str, dict[str, Any]] = {}
        self._external_seen: set[str] = set()
        self._external_bootstrapped = False
        self._last_external_poll_ts = 0.0
        self._default_notify_route: tuple[str, str] | None = None
        self._default_work_dir: str | None = (
            str(Path(default_work_dir).expanduser()) if default_work_dir else os.getcwd()
        )

        logs_dir = Path(config.logs_dir) if config.logs_dir else get_data_path() / "codex_runs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir = logs_dir
        self._state_path = logs_dir / "runs.json"
        self._notify_state_path = logs_dir / "notify_route.json"
        self._load_state()
        self._load_notify_state()

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        logger.info("Codex service started")
        while self._running:
            try:
                self._poll_external_sessions()
            except Exception as e:
                logger.debug(f"External codex poll skipped: {e}")
            await asyncio.sleep(1)

    def stop(self) -> None:
        self._running = False
        self._save_state()
        logger.info("Codex service stopping")

    def list_runs_text(self, limit: int = 20) -> str:
        runs = list(self._runs.values())
        if not runs:
            return "No codex runs tracked."
        runs = sorted(runs, key=lambda r: r.started_at, reverse=True)[:limit]
        lines = []
        for r in runs:
            lines.append(
                f"{r.name} ({r.run_id}): status={r.status}, pid={r.pid}, started={r.started_at}"
            )
        return "\n".join(lines)

    def get_run(self, name_or_id: str) -> CodexRun | None:
        if name_or_id in self._runs:
            return self._runs[name_or_id]
        for r in self._runs.values():
            if r.name == name_or_id:
                return r
        return None

    def tail_run(self, name_or_id: str, lines: int = 40) -> str:
        run = self.get_run(name_or_id)
        if not run:
            return "Run not found."
        return self._tail_file(run.log_path, lines=lines)

    def stop_run(self, name_or_id: str) -> str:
        run = self.get_run(name_or_id)
        if not run:
            return "Run not found."
        if run.status != "running":
            return f"Run {run.name} is not running."
        proc = self._get_proc(run.run_id)
        if not proc:
            run.status = "stopped"
            self._save_state()
            return f"Run {run.name} stopped (process not found)."
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        run.status = "stopped"
        run.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()
        self._notify(
            run,
            f"### Stopped\n\nRun `{run.name}` (`{run.run_id}`) has been stopped.\n\n{self._quick_actions(run)}",
            fmt="markdown",
        )
        return f"Stopped {run.name}."

    def run_exec(
        self,
        prompt: str,
        name: str | None,
        channel: str,
        chat_id: str,
        extra_args: list[str] | None = None,
    ) -> CodexRun:
        args = ["exec"]
        args += self.config.exec_args
        return self._start_run(
            name=name,
            base_args=args,
            prompt=prompt,
            channel=channel,
            chat_id=chat_id,
            extra_args=extra_args or [],
            json_output=True,
        )

    def run_review(
        self,
        prompt: str,
        name: str | None,
        channel: str,
        chat_id: str,
        extra_args: list[str] | None = None,
    ) -> CodexRun:
        args = ["review"]
        args += self.config.review_args
        return self._start_run(
            name=name,
            base_args=args,
            prompt=prompt,
            channel=channel,
            chat_id=chat_id,
            extra_args=extra_args or [],
            json_output=False,
        )

    def run_resume(
        self,
        session_id: str | None,
        prompt: str,
        name: str | None,
        channel: str,
        chat_id: str,
        extra_args: list[str] | None = None,
    ) -> CodexRun:
        args = ["exec", "resume"]
        if session_id:
            args.append(session_id)
        args += self.config.resume_args
        resume_cwd = self._resolve_resume_cwd(session_id)
        return self._start_run(
            name=name,
            base_args=args,
            prompt=prompt,
            channel=channel,
            chat_id=chat_id,
            extra_args=extra_args or [],
            json_output=True,
            work_dir=resume_cwd,
        )

    def run_apply(
        self,
        task_id: str,
        name: str | None,
        channel: str,
        chat_id: str,
        extra_args: list[str] | None = None,
    ) -> CodexRun:
        args = ["apply", task_id]
        args += self.config.apply_args
        return self._start_run(
            name=name,
            base_args=args,
            prompt="",
            channel=channel,
            chat_id=chat_id,
            extra_args=extra_args or [],
            json_output=False,
        )

    def list_sessions_text(self, limit: int = 10) -> str:
        sessions = self._list_codex_sessions(limit)
        if not sessions:
            return "No codex sessions found."
        lines = []
        for s in sessions:
            lines.append(f"{s.get('id')} | {s.get('timestamp')} | {s.get('cwd')}")
        return "\n".join(lines)

    def diff_files_text(self, target: str | None = None) -> str:
        label, cwd = self._resolve_diff_target(target)
        if not cwd:
            return "No target workspace found. Try `/cx list` or `/cx sessions 5` first."
        workspace = str(Path(cwd).expanduser())
        status = self._git_status_short(workspace)
        if status is None:
            return "\n".join([
                "### Diff Files",
                "",
                f"- Target: `{label}`",
                f"- Workspace: `{workspace}`",
                "",
                "No git repository found in this workspace.",
            ])
        lines = [ln for ln in status.splitlines() if ln.strip()]
        if not lines:
            return "\n".join([
                "### Diff Files",
                "",
                f"- Target: `{label}`",
                f"- Workspace: `{workspace}`",
                "",
                "No changed files.",
            ])
        max_items = 200
        shown = lines[:max_items]
        body = "\n".join(shown)
        more = ""
        if len(lines) > max_items:
            more = f"\n... ({len(lines) - max_items} more)"
        return (
            f"### Diff Files\n\n"
            f"- Target: `{label}`\n"
            f"- Workspace: `{workspace}`\n\n"
            f"```text\n{body}{more}\n```"
        )

    def list_pending_text(self) -> str:
        pending = [
            r for r in self._runs.values()
            if r.status == "running" and r.awaiting_approval
        ]
        if not pending:
            return "No pending approvals."
        pending = sorted(pending, key=lambda r: r.started_at, reverse=True)
        lines = ["### Pending Approvals", ""]
        for r in pending:
            prompt = self._trim_output(r.approval_prompt or "")
            lines.extend([
                f"- Run: `{r.name}` (`{r.run_id}`)",
                f"- Workspace: `{r.cwd}`",
                f"- Prompt: {prompt or '(empty)'}",
                f"- Actions:",
                f"/cx approve {r.name}",
                f"/cx reject {r.name}",
                "",
            ])
        return "\n".join(lines).rstrip()

    def submit_approval(self, name_or_id: str, approved: bool) -> str:
        run = self.get_run(name_or_id)
        if not run:
            return "Run not found."
        if run.status != "running":
            return f"Run {run.name} is not running."
        if not run.awaiting_approval:
            return f"Run {run.name} has no pending approval request."
        proc = self._get_proc(run.run_id)
        if not proc or not proc.stdin:
            return f"Run {run.name} cannot accept approval input right now."
        token = "y\n" if approved else "n\n"
        try:
            proc.stdin.write(token)
            proc.stdin.flush()
        except Exception as e:
            return f"Failed to send approval decision: {e}"
        run.awaiting_approval = False
        run.approval_prompt = ""
        run.approval_requested_at = None
        self._save_state()
        action = "approved" if approved else "rejected"
        return f"Sent {action} decision to `{run.name}`."

    def set_stream_enabled(self, enabled: bool) -> None:
        self.config.stream.enabled = enabled

    def bind_notify_target(self, channel: str, chat_id: str) -> None:
        self._default_notify_route = (channel, chat_id)
        self._save_notify_state()

    def unbind_notify_target(self) -> None:
        self._default_notify_route = None
        self._save_notify_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def get_effective_workdir(self) -> str:
        if self.config.work_dir:
            return str(Path(self.config.work_dir).expanduser())
        if self._default_work_dir:
            return self._default_work_dir
        return os.getcwd()

    def _allocate_temp_workspace(self, run_id: str) -> str:
        if self.config.temp_workspace_root:
            root = Path(self.config.temp_workspace_root).expanduser()
        else:
            root = Path(self.get_effective_workdir()) / ".codex-temp-workspaces"
        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / f"run-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return str(run_dir)

    def _reasoning_effort_value(self) -> str | None:
        mode = (self.config.reasoning_mode or "").strip().lower()
        if not mode:
            return None
        if mode == "xhigh":
            return "high"
        if mode in ("high", "medium", "low"):
            return mode
        return None

    def _start_run(
        self,
        name: str | None,
        base_args: list[str],
        prompt: str,
        channel: str,
        chat_id: str,
        extra_args: list[str],
        json_output: bool,
        work_dir: str | None = None,
    ) -> CodexRun:
        if self.config.max_running > 0:
            running = sum(1 for r in self._runs.values() if r.status == "running")
            if running >= self.config.max_running:
                raise RuntimeError("Too many running codex tasks.")
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        codex_exec = self._resolve_codex_path(self.config.codex_path)
        if not codex_exec:
            raise RuntimeError(
                "codex executable not found. Set codex.codexPath to the full path "
                "(e.g. C:\\Users\\<you>\\AppData\\Roaming\\npm\\codex.cmd)."
            )

        run_id = self._make_run_id()
        run_name = name or f"codex-{run_id[-6:]}"
        log_path = self._logs_dir / f"{run_id}.log"

        cmd = [codex_exec]
        cmd += self.config.default_args
        if self.config.model:
            cmd += ["--model", self.config.model]
        if self.config.sandbox:
            cmd += ["--sandbox", self.config.sandbox]
        if self.config.approval_policy:
            cmd += ["--ask-for-approval", self.config.approval_policy]
        if effort := self._reasoning_effort_value():
            cmd += ["-c", f'reasoning_effort="{effort}"']
        cmd += base_args
        # --skip-git-repo-check must be placed after subcommand (e.g. "exec --skip-git-repo-check")
        if "--skip-git-repo-check" not in cmd and base_args:
            sub = base_args[0]
            if sub in ("exec", "review"):
                insert_at = len(cmd) - len(base_args) + 1
                cmd.insert(insert_at, "--skip-git-repo-check")
        if json_output:
            cmd.append("--json")
        if extra_args:
            cmd += extra_args
        if prompt:
            cmd.append(prompt)

        if work_dir:
            run_cwd = str(Path(work_dir).expanduser())
        elif self.config.use_temp_workspace:
            run_cwd = self._allocate_temp_workspace(run_id)
        else:
            run_cwd = self.get_effective_workdir()
        run = CodexRun(
            run_id=run_id,
            name=run_name,
            cmd=cmd,
            cwd=run_cwd,
            log_path=log_path,
            channel=channel,
            chat_id=chat_id,
            is_json=json_output,
        )
        self.bind_notify_target(channel, chat_id)

        proc = subprocess.Popen(
            cmd,
            cwd=run.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        run.pid = proc.pid
        self._register_proc(run.run_id, proc)

        with self._lock:
            self._runs[run.run_id] = run
            self._save_state()

        thread = threading.Thread(target=self._read_output, args=(run, proc), daemon=True)
        thread.start()
        return run

    def _read_output(self, run: CodexRun, proc: subprocess.Popen) -> None:
        try:
            with run.log_path.open("a", encoding="utf-8") as f:
                if not proc.stdout:
                    return
                for line in proc.stdout:
                    f.write(line)
                    f.flush()
                    self._handle_line(run, line)
        except Exception as e:
            logger.error(f"Codex output reader failed: {e}")
        finally:
            exit_code = proc.wait()
            run.exit_code = exit_code
            run.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            run.awaiting_approval = False
            if run.status == "running":
                run.status = "finished" if exit_code == 0 else "failed"
            if run.stream_pending and self.config.stream.enabled:
                self._flush_stream(run, time.time())
            self._save_state()
            if run.last_answer:
                answer_text = self._trim_output(run.last_answer)
                self._notify(
                    run,
                    f"{answer_text}\n\n{self._context_footer(run)}",
                    fmt="markdown",
                )
            if exit_code != 0:
                reason = self._trim_output(run.last_error_text) if run.last_error_text else ""
                if reason:
                    content = (
                        f"### Run Failed\n\n"
                        f"- Run: `{run.name}`\n"
                        f"- Exit: `{exit_code}`\n\n"
                        f"**Reason**\n```\n{reason}\n```\n\n"
                        f"{self._quick_actions(run)}"
                    )
                else:
                    content = (
                        f"### Run Failed\n\n"
                        f"- Run: `{run.name}`\n"
                        f"- Exit: `{exit_code}`\n\n"
                        f"{self._quick_actions(run)}"
                    )
                self._notify(
                    run,
                    content,
                    fmt="markdown",
                )
            else:
                self._notify(
                    run,
                    "### Turn Finished\n\n" + self._quick_actions(run),
                    fmt="markdown",
                )

    def _handle_line(self, run: CodexRun, line: str) -> None:
        raw = (line or "").strip()
        if not raw:
            return
        text = None
        answer = None
        approval_prompt = None
        if run.is_json:
            try:
                obj = json.loads(raw)
                sid = self._extract_session_id(obj)
                if sid:
                    run.session_id = sid
                    self._mark_owned_session_id(sid)
                text = self._extract_text(obj)
                answer = self._extract_answer(obj)
                if answer:
                    run.last_answer = answer
                approval_prompt = self._extract_approval_prompt(obj)
            except Exception:
                run.last_error_text = raw
                text = None
        else:
            text = raw
            run.last_error_text = text
        if not approval_prompt:
            approval_prompt = self._extract_approval_prompt_from_text(raw)
        if approval_prompt:
            self._set_pending_approval(run, approval_prompt)
        if not text:
            return
        if not self.config.stream.enabled or not self._loop:
            return
        run.stream_pending = self._append_pending(run.stream_pending, text)
        now = time.time()
        if (now - run.last_stream_ts) >= self.config.stream.min_interval_seconds:
            self._flush_stream(run, now)
        elif len(run.stream_pending) >= self.config.stream.max_chars:
            self._flush_stream(run, now)

    def _flush_stream(self, run: CodexRun, now: float) -> None:
        if not run.stream_pending:
            return
        content = self._trim_output(run.stream_pending)
        run.stream_pending = ""
        run.last_stream_ts = now
        if content:
            self._notify(run, content, fmt="markdown")

    def _notify(
        self,
        run: CodexRun,
        content: str,
        fmt: str = "markdown",
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        target_channel = channel or run.channel
        target_chat_id = chat_id or run.chat_id
        if not target_channel or not target_chat_id:
            return
        self._notify_route(target_channel, target_chat_id, content, fmt=fmt)

    def _notify_route(self, channel: str, chat_id: str, content: str, fmt: str = "markdown") -> None:
        if not self._loop:
            return
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={"format": fmt, "source": "codex"},
        )
        asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(msg), self._loop)

    @staticmethod
    def _pick_format(content: str) -> str:
        text = content or ""
        if "```" in text:
            return "markdown"
        if "\n#" in text or text.startswith("#"):
            return "markdown"
        if "\n- " in text or "\n* " in text:
            return "markdown"
        if "\n| " in text and "\n|-" in text:
            return "markdown"
        return "text"

    def _context_footer(self, run: CodexRun) -> str:
        session = run.session_id or "N/A"
        return (
            f"---\n"
            f"**Workspace:** `{run.cwd}`\n"
            f"**Session:** `{session}`"
        )

    def _quick_actions(self, run: CodexRun) -> str:
        if run.session_id:
            commands = [
                f"/cx resume {run.session_id} [your prompt]",
                f"/cx tail {run.name}",
            ]
        else:
            commands = [
                "/cx sessions 5",
                "/cx resume [session_id] [your prompt]",
                f"/cx tail {run.name}",
            ]
        cmd_block = "\n".join(commands)
        return (
            f"{self._context_footer(run)}\n\n"
            f"**Quick Continue**\n"
            f"Copy and send one command:\n"
            f"{cmd_block}"
        )

    @staticmethod
    def _append_pending(pending: str, text: str) -> str:
        if pending:
            return pending + "\n" + text
        return text

    def _trim_output(self, text: str) -> str:
        lines = text.splitlines()[-self.config.stream.max_lines :]
        out = "\n".join(lines)
        if len(out) > self.config.stream.max_chars:
            out = out[-self.config.stream.max_chars :]
        return out.strip()

    @staticmethod
    def _resolve_codex_path(candidate: str) -> str | None:
        """Resolve codex executable path with Windows-friendly fallbacks."""
        if not candidate:
            return None
        # Direct path
        if Path(candidate).exists():
            return candidate
        # PATH lookup
        found = shutil.which(candidate)
        if found:
            return found
        # Windows: try .cmd / .exe and common npm global path
        if os.name == "nt":
            for suffix in (".cmd", ".exe"):
                found = shutil.which(candidate + suffix)
                if found:
                    return found
            appdata = os.environ.get("APPDATA") or ""
            if appdata:
                p = Path(appdata) / "npm" / "codex.cmd"
                if p.exists():
                    return str(p)
            p = Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"
            if p.exists():
                return str(p)
        return None

    @staticmethod
    def _extract_text(obj: dict[str, Any]) -> str | None:
        typ = obj.get("type")
        payload = obj.get("payload") or {}
        if typ == "item.completed":
            item = obj.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                return item.get("text") or ""
            if itype == "agent_reasoning":
                return item.get("text") or ""
        if typ == "error":
            return obj.get("message") or ""
        if typ == "turn.failed":
            err = obj.get("error") or {}
            return err.get("message") or ""
        if typ == "event_msg":
            ptype = payload.get("type")
            if ptype == "agent_message":
                return payload.get("message") or ""
            if ptype == "agent_reasoning":
                return payload.get("text") or ""
        if typ == "response_item":
            if payload.get("type") == "message" and payload.get("role") in ("assistant", "agent"):
                content = payload.get("content") or []
                texts = []
                for item in content:
                    itype = item.get("type")
                    if itype in ("output_text", "text"):
                        texts.append(item.get("text") or "")
                if texts:
                    return "\n".join(texts)
        return None

    @staticmethod
    def _extract_answer(obj: dict[str, Any]) -> str | None:
        """Extract the final assistant message text when present."""
        typ = obj.get("type")
        if typ == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message":
                return item.get("text") or None
            return None
        if typ == "event_msg":
            payload = obj.get("payload") or {}
            if payload.get("type") == "task_complete":
                return payload.get("last_agent_message") or None
            return None
        if typ != "response_item":
            return None
        payload = obj.get("payload") or {}
        if payload.get("type") != "message" or payload.get("role") not in ("assistant", "agent"):
            return None
        content = payload.get("content") or []
        texts = []
        for item in content:
            itype = item.get("type")
            if itype in ("output_text", "text"):
                texts.append(item.get("text") or "")
        out = "\n".join([t for t in texts if t])
        return out or None

    @staticmethod
    def _extract_session_id(obj: dict[str, Any]) -> str | None:
        typ = obj.get("type")
        if typ == "thread.started":
            return obj.get("thread_id")
        if typ == "session_meta":
            payload = obj.get("payload") or {}
            return payload.get("id")
        payload = obj.get("payload") or {}
        if typ == "event_msg" and payload.get("session_id"):
            return payload.get("session_id")
        return None

    @staticmethod
    def _extract_approval_prompt_from_text(text: str) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        low = t.lower()
        has_approval_word = any(k in low for k in ("approve", "approval", "permission", "allow this"))
        has_choice_word = any(k in low for k in ("y/n", "yes/no", "(y/n)", "[y/n]"))
        if has_approval_word and has_choice_word:
            return t
        if re.search(r"\bapprove\b.*\?", low):
            return t
        return None

    @classmethod
    def _extract_approval_prompt(cls, obj: dict[str, Any]) -> str | None:
        typ = str(obj.get("type") or "").lower()
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ptype = str(payload.get("type") or "").lower()
        if "approval" in typ or "approval" in ptype:
            return cls._extract_first_text(obj) or f"Approval requested ({typ or ptype})"
        if cls._json_has_approval_flag(obj):
            return cls._extract_first_text(obj) or "Approval required by codex."
        return None

    @classmethod
    def _json_has_approval_flag(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for k, v in value.items():
                lk = str(k).lower()
                if "approval" in lk or "approve" in lk:
                    if isinstance(v, bool) and v:
                        return True
                    if isinstance(v, str) and v.strip():
                        return True
                if cls._json_has_approval_flag(v):
                    return True
            return False
        if isinstance(value, list):
            return any(cls._json_has_approval_flag(v) for v in value)
        return False

    @classmethod
    def _extract_first_text(cls, value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("message", "text", "prompt", "reason", "description"):
                v = value.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in value.values():
                found = cls._extract_first_text(v)
                if found:
                    return found
            return None
        if isinstance(value, list):
            for item in value:
                found = cls._extract_first_text(item)
                if found:
                    return found
            return None
        return None

    def _set_pending_approval(self, run: CodexRun, prompt: str) -> None:
        prompt_clean = self._trim_output(prompt or "") or "Approval required by codex."
        if run.awaiting_approval and run.approval_prompt == prompt_clean:
            return
        run.awaiting_approval = True
        run.approval_prompt = prompt_clean
        run.approval_requested_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()
        message = (
            f"### Approval Required\n\n"
            f"- Run: `{run.name}`\n"
            f"- Workspace: `{run.cwd}`\n\n"
            f"**Request**\n{prompt_clean}\n\n"
            f"**Actions**\n"
            f"/cx approve {run.name}\n"
            f"/cx reject {run.name}\n"
            f"/cx pending"
        )
        self._notify(run, message, fmt="markdown")

    @staticmethod
    def _session_id_variants(session_id: str) -> set[str]:
        sid = (session_id or "").strip()
        if not sid:
            return set()
        return {
            sid,
            sid.replace("-", "_"),
            sid.replace("_", "-"),
        }

    def _mark_owned_session_id(self, session_id: str | None) -> None:
        if not session_id:
            return
        for sid in self._session_id_variants(session_id):
            self._owned_session_ids.add(sid)

    def _list_codex_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        base = Path.home() / ".codex" / "sessions"
        if not base.exists():
            return []
        files = list(base.rglob("rollout-*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[dict[str, Any]] = []
        for path in files[:limit]:
            meta = self._read_session_meta(path)
            if meta:
                out.append(meta)
        return out

    def _resolve_resume_cwd(self, session_id: str | None) -> str | None:
        """
        Resolve working directory for resume:
        1) exact session_id match if provided
        2) most recent session cwd
        """
        if session_id:
            if meta := self._find_session_meta(session_id):
                cwd = meta.get("cwd")
                if cwd:
                    return str(cwd)
        recent = self._list_codex_sessions(limit=1)
        if recent and recent[0].get("cwd"):
            return str(recent[0]["cwd"])
        return None

    def _resolve_diff_target(self, target: str | None) -> tuple[str, str | None]:
        raw = (target or "").strip()
        if raw:
            if run := self.get_run(raw):
                return (f"run:{run.name}", run.cwd)
            if meta := self._find_session_meta(raw):
                cwd = meta.get("cwd")
                return (f"session:{raw}", str(cwd) if cwd else None)
            direct = str(Path(raw).expanduser())
            return (f"path:{raw}", direct)

        runs = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        if runs:
            return (f"run:{runs[0].name}", runs[0].cwd)
        recent = self._list_codex_sessions(limit=1)
        if recent:
            sid = recent[0].get("id") or "latest"
            cwd = recent[0].get("cwd")
            return (f"session:{sid}", str(cwd) if cwd else None)
        return ("latest", None)

    @staticmethod
    def _git_status_short(workspace: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", workspace, "status", "--short"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def _find_session_meta(self, session_id: str) -> dict[str, Any] | None:
        base = Path.home() / ".codex" / "sessions"
        if not base.exists():
            return None
        files = list(base.rglob("rollout-*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            meta = self._read_session_meta(path)
            if not meta:
                continue
            if meta.get("id") == session_id:
                return meta
        return None

    @staticmethod
    def _read_session_meta(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                line = f.readline().strip()
            if not line:
                return None
            obj = json.loads(line)
            if obj.get("type") != "session_meta":
                return None
            payload = obj.get("payload") or {}
            return {
                "id": payload.get("id"),
                "timestamp": payload.get("timestamp"),
                "cwd": payload.get("cwd"),
            }
        except Exception:
            return None

    def _poll_external_sessions(self) -> None:
        if not self._loop:
            return
        now = time.time()
        if (now - self._last_external_poll_ts) < 3:
            return
        self._last_external_poll_ts = now

        files = self._iter_external_files(limit=50)
        if not self._external_bootstrapped:
            for path in files:
                try:
                    self._external_offsets[str(path)] = path.stat().st_size
                except Exception:
                    continue
            self._external_bootstrapped = True
            return

        for path in files:
            key = str(path)
            try:
                size = path.stat().st_size
            except Exception:
                continue
            offset = self._external_offsets.get(key, 0)
            if size < offset:
                offset = 0
            if size == offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    if offset > 0:
                        f.seek(offset)
                    for raw in f:
                        self._handle_external_line(path, raw.strip())
                self._external_offsets[key] = size
            except Exception:
                continue

        if len(self._external_offsets) > 500:
            keep = {str(p) for p in files}
            self._external_offsets = {k: v for k, v in self._external_offsets.items() if k in keep}
        if len(self._external_seen) > 2000:
            self._external_seen.clear()

    @staticmethod
    def _iter_external_files(limit: int = 50) -> list[Path]:
        base = Path.home() / ".codex" / "sessions"
        if not base.exists():
            return []
        files = list(base.rglob("rollout-*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]

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
        if obj.get("type") != "event_msg":
            return
        payload = obj.get("payload") or {}
        if payload.get("type") != "task_complete":
            return
        if not sid:
            return
        meta = self._find_external_meta(sid)
        if not meta:
            # Fallback: read session meta from the same rollout file.
            if file_meta := self._read_session_meta(path):
                meta = file_meta
                if file_meta.get("id"):
                    self._external_meta[file_meta["id"]] = file_meta
        cwd = (meta or {}).get("cwd")
        if self._is_owned_or_recent_local(sid, cwd):
            logger.info(f"Skip external duplicate codex turn: session={sid}, cwd={cwd or 'N/A'}")
            return
        event_key = f"{sid}:{payload.get('turn_id') or obj.get('timestamp') or ''}"
        if event_key in self._external_seen:
            return
        self._external_seen.add(event_key)
        route = self._get_notify_route()
        if not route:
            return
        channel, chat_id = route
        last_msg = (payload.get("last_agent_message") or "").strip()
        cwd = cwd or "N/A"
        if last_msg:
            answer_msg = (
                f"{last_msg}\n\n"
                f"---\n"
                f"**Workspace:** `{cwd}`\n"
                f"**Session:** `{sid}`"
            )
            self._notify_route(channel, chat_id, answer_msg, fmt="markdown")
        summary_msg = (
            f"### External Turn Finished\n\n"
            f"**Workspace:** `{cwd}`\n"
            f"**Session:** `{sid}`\n\n"
            f"**Quick Continue**\n"
            f"Copy and send one command:\n"
            f"/cx resume {sid} [your prompt]\n"
            f"/cx sessions 5"
        )
        self._notify_route(channel, chat_id, summary_msg, fmt="markdown")

    @staticmethod
    def _extract_session_id_from_path(path: Path) -> str | None:
        name = path.stem
        # rollout-2026-02-15T19-00-11-<session_id>
        m = re.match(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$", name)
        if m:
            return m.group(1).strip() or None
        if not name.startswith("rollout-"):
            return None
        # Fallback parser: keep everything after timestamp as session id.
        parts = name.split("-", 6)
        if len(parts) < 7:
            return None
        sid = parts[6].strip()
        return sid or None

    def _find_external_meta(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        for sid in self._session_id_variants(session_id):
            if sid in self._external_meta:
                return self._external_meta[sid]
        return None

    @staticmethod
    def _normalize_path(path: str | None) -> str:
        if not path:
            return ""
        try:
            return os.path.normcase(os.path.normpath(str(Path(path).expanduser())))
        except Exception:
            return os.path.normcase(os.path.normpath(str(path)))

    @staticmethod
    def _is_run_recent(run: CodexRun, seconds: int = 1800) -> bool:
        if run.status == "running":
            return True
        ts = run.finished_at or run.started_at
        if not ts:
            return False
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False
        return (datetime.now() - dt).total_seconds() <= seconds

    def _is_owned_or_recent_local(self, session_id: str | None, cwd: str | None) -> bool:
        if session_id:
            variants = self._session_id_variants(session_id)
            if any(v in self._owned_session_ids for v in variants):
                return True
            for run in self._runs.values():
                if not run.session_id:
                    continue
                if variants & self._session_id_variants(run.session_id):
                    return True
        norm_cwd = self._normalize_path(cwd)
        if norm_cwd:
            for run in self._runs.values():
                if not self._is_run_recent(run):
                    continue
                if self._normalize_path(run.cwd) == norm_cwd:
                    return True
        return False

    def _get_notify_route(self) -> tuple[str, str] | None:
        if self._default_notify_route:
            return self._default_notify_route
        for r in sorted(self._runs.values(), key=lambda x: x.started_at, reverse=True):
            if r.channel and r.chat_id:
                return (r.channel, r.chat_id)
        return None

    def _load_notify_state(self) -> None:
        if not self._notify_state_path.exists():
            return
        try:
            raw = json.loads(self._notify_state_path.read_text(encoding="utf-8"))
            channel = raw.get("channel")
            chat_id = raw.get("chat_id")
            if channel and chat_id:
                self._default_notify_route = (channel, chat_id)
        except Exception:
            return

    def _save_notify_state(self) -> None:
        data: dict[str, Any] = {}
        if self._default_notify_route:
            data = {
                "channel": self._default_notify_route[0],
                "chat_id": self._default_notify_route[1],
            }
        try:
            self._notify_state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _tail_file(self, path: Path, lines: int = 40, max_chars: int = 4000) -> str:
        if not path.exists():
            return "Log file not found."
        data = b""
        block = 4096
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            while end > 0 and data.count(b"\n") <= lines:
                offset = max(0, end - block)
                f.seek(offset)
                chunk = f.read(end - offset)
                data = chunk + data
                end = offset
                if end == 0:
                    break
        text = data.decode("utf-8", errors="ignore")
        out_lines = text.splitlines()[-lines:]
        out = "\n".join(out_lines)
        if len(out) > max_chars:
            out = out[-max_chars:]
        return out if out else "(empty)"

    def _make_run_id(self) -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            for item in raw:
                run = CodexRun(
                    run_id=item.get("run_id", ""),
                    name=item.get("name", ""),
                    cmd=item.get("cmd", []),
                    cwd=item.get("cwd") or self.get_effective_workdir(),
                    log_path=Path(item.get("log_path", "")),
                    status=item.get("status", "unknown"),
                    pid=item.get("pid"),
                    started_at=item.get("started_at", ""),
                    finished_at=item.get("finished_at"),
                    exit_code=item.get("exit_code"),
                    channel=item.get("channel", ""),
                    chat_id=item.get("chat_id", ""),
                    session_id=item.get("session_id"),
                    is_json=item.get("is_json", False),
                    awaiting_approval=item.get("awaiting_approval", False),
                    approval_prompt=item.get("approval_prompt", ""),
                    approval_requested_at=item.get("approval_requested_at"),
                )
                if run.run_id:
                    self._runs[run.run_id] = run
                    self._mark_owned_session_id(run.session_id)
        except Exception:
            return

    def _save_state(self) -> None:
        items = []
        for r in self._runs.values():
            items.append({
                "run_id": r.run_id,
                "name": r.name,
                "cmd": r.cmd,
                "cwd": r.cwd,
                "log_path": str(r.log_path),
                "status": r.status,
                "pid": r.pid,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "exit_code": r.exit_code,
                "channel": r.channel,
                "chat_id": r.chat_id,
                "session_id": r.session_id,
                "is_json": r.is_json,
                "awaiting_approval": r.awaiting_approval,
                "approval_prompt": r.approval_prompt,
                "approval_requested_at": r.approval_requested_at,
            })
        try:
            self._state_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _register_proc(self, run_id: str, proc: subprocess.Popen) -> None:
        # Store the proc in a private attribute to avoid serialization
        setattr(self, f"_proc_{run_id}", proc)

    def _get_proc(self, run_id: str) -> subprocess.Popen | None:
        return getattr(self, f"_proc_{run_id}", None)
