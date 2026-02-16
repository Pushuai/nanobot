"""Shell execution tool."""

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool


class ExecTool(Tool):
    """Tool to execute shell commands."""
    
    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        approval_enabled: bool = True,
        approval_risk_patterns: list[str] | None = None,
        risk_assessor: Callable[[str, str], Awaitable[dict[str, Any] | None]] | None = None,
        approval_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        restrict_to_workspace: bool = False,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"\b(format|mkfs|diskpart)\b",   # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.approval_enabled = approval_enabled
        self.approval_risk_patterns = approval_risk_patterns or [
            r"\b(rm|mv|cp|rename|ren|del|rmdir|mkdir|md|touch)\b",
            r"\b(chmod|chown|takeown|icacls)\b",
            r"\b(git\s+(add|commit|push|reset|clean|checkout))\b",
            r"\b(pip|pip3|uv|npm|pnpm|yarn|apt|yum|brew|choco)\s+(install|uninstall|remove|upgrade|update)\b",
            r"\b(docker\s+(rm|rmi|compose\s+down|compose\s+rm|system\s+prune))\b",
            r"\b(sed\s+-i|perl\s+-i)\b",
            r"(^|[^2])>>?",
        ]
        self._risk_assessor = risk_assessor
        self._approval_callback = approval_callback
        self.restrict_to_workspace = restrict_to_workspace
        self._ctx_channel = ""
        self._ctx_chat_id = ""
        self._ctx_sender_id = ""
        self._pending: dict[str, dict[str, Any]] = {}
    
    @property
    def name(self) -> str:
        return "exec"
    
    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }
    
    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        regex_hit = self._needs_approval(command)
        model_risk = await self._assess_model_risk(command, cwd)
        model_hit = bool(model_risk and model_risk.get("require_approval"))

        if self.approval_enabled and (regex_hit or model_hit):
            req = self._create_pending_request(command, cwd)
            req["risk_meta"] = {
                "regex_hit": regex_hit,
                "model": model_risk or {},
            }
            await self._notify_approval(req)
            triggers: list[str] = []
            if regex_hit:
                triggers.append("regex")
            if model_hit:
                level = str((model_risk or {}).get("level") or "unknown")
                triggers.append(f"model:{level}")
            trigger_line = f"- Triggered by: {', '.join(triggers)}\n" if triggers else ""
            model_line = ""
            if model_risk and model_risk.get("reason"):
                level = str(model_risk.get("level") or "unknown")
                reason = str(model_risk.get("reason") or "").strip()
                if len(reason) > 280:
                    reason = reason[:280] + "..."
                model_line = f"- Model risk: {level} ({reason})\n"
            return (
                "Command requires approval before execution.\n"
                f"- Request: {req['id']}\n"
                f"- Command: {command}\n"
                f"{trigger_line}"
                f"{model_line}"
                f"- Approve: /exec approve {req['id']}\n"
                f"- Reject: /exec reject {req['id']}\n"
                "- Pending list: /exec pending"
            )

        return await self._run_command(command, cwd)

    async def _run_command(self, command: str, cwd: str) -> str:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                return f"Error: Command timed out after {self.timeout} seconds"
            
            output_parts = []
            
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")
            
            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")
            
            result = "\n".join(output_parts) if output_parts else "(no output)"
            
            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
            
            return result
            
        except Exception as e:
            return f"Error executing command: {str(e)}"

    def set_context(self, channel: str, chat_id: str, sender_id: str | None = None) -> None:
        self._ctx_channel = channel
        self._ctx_chat_id = chat_id
        self._ctx_sender_id = sender_id or ""

    async def resolve_pending(self, request_id: str, approved: bool, actor_id: str | None = None) -> str:
        req = self._pending.get(request_id)
        if not req:
            return f"Pending request not found: {request_id}"
        if req.get("status") != "pending":
            return f"Request {request_id} is not pending."
        owner = req.get("sender_id") or ""
        if owner and actor_id and owner != actor_id:
            return "Only the request initiator can approve/reject this command."
        if not approved:
            req["status"] = "rejected"
            self._pending.pop(request_id, None)
            return f"Rejected command request `{request_id}`."

        req["status"] = "executing"
        output = await self._run_command(req["command"], req["cwd"])
        self._pending.pop(request_id, None)
        return (
            f"Approved and executed `{request_id}`.\n"
            f"Command: {req['command']}\n\n"
            f"{output}"
        )

    def list_pending_text(self, chat_id: str | None = None) -> str:
        pending = [
            p for p in self._pending.values()
            if p.get("status") == "pending" and (not chat_id or p.get("chat_id") == chat_id)
        ]
        if not pending:
            return "No pending exec approvals."
        lines = ["### Pending Exec Approvals", ""]
        for p in pending:
            risk_text = self._format_risk_meta(p.get("risk_meta"))
            lines.extend([
                f"- Request: `{p['id']}`",
                f"- Command: `{p['command']}`",
                f"- Workspace: `{p['cwd']}`",
                *([f"- Risk: {risk_text}"] if risk_text else []),
                f"- Approve: `/exec approve {p['id']}`",
                f"- Reject: `/exec reject {p['id']}`",
                "",
            ])
        return "\n".join(lines).rstrip()

    def _needs_approval(self, command: str) -> bool:
        lower = command.strip().lower()
        if not lower:
            return False
        for pattern in self.approval_risk_patterns:
            if re.search(pattern, lower):
                return True
        return False

    def _create_pending_request(self, command: str, cwd: str) -> dict[str, Any]:
        rid = uuid.uuid4().hex[:10]
        req = {
            "id": rid,
            "command": command,
            "cwd": cwd,
            "channel": self._ctx_channel,
            "chat_id": self._ctx_chat_id,
            "sender_id": self._ctx_sender_id,
            "status": "pending",
        }
        self._pending[rid] = req
        return req

    async def _assess_model_risk(self, command: str, cwd: str) -> dict[str, Any] | None:
        if not self._risk_assessor:
            return None
        try:
            result = await self._risk_assessor(command, cwd)
            if not isinstance(result, dict):
                return None
            return {
                "source": str(result.get("source") or "model"),
                "level": str(result.get("level") or "unknown").lower(),
                "reason": str(result.get("reason") or "").strip(),
                "require_approval": bool(result.get("require_approval")),
            }
        except Exception:
            return {
                "source": "model",
                "level": "unknown",
                "reason": "risk assessor failed",
                "require_approval": False,
            }

    @staticmethod
    def _format_risk_meta(meta: Any) -> str:
        if not isinstance(meta, dict):
            return ""
        chunks: list[str] = []
        if meta.get("regex_hit"):
            chunks.append("regex matched")
        model = meta.get("model")
        if isinstance(model, dict):
            level = str(model.get("level") or "").strip()
            reason = str(model.get("reason") or "").strip()
            if level:
                if reason:
                    if len(reason) > 140:
                        reason = reason[:140] + "..."
                    chunks.append(f"model={level} ({reason})")
                else:
                    chunks.append(f"model={level}")
        return "; ".join(chunks)

    async def _notify_approval(self, req: dict[str, Any]) -> None:
        if not self._approval_callback:
            return
        try:
            await self._approval_callback(req)
        except Exception:
            pass

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            # Only match absolute paths — avoid false positives on relative
            # paths like ".venv/bin/python" where "/bin/python" would be
            # incorrectly extracted by the old pattern.
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None
