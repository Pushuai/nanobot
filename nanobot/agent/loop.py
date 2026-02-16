"""Agent loop: the core processing engine."""

import asyncio
from contextlib import AsyncExitStack
import json
import re
import shlex
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.session.manager import Session, SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        codex_service: "CodexService | None" = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        from nanobot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.codex = codex_service

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        
        # Shell tool
        deny_patterns = self.exec_config.deny_patterns or None
        allow_patterns = self.exec_config.allow_patterns or None
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            deny_patterns=deny_patterns,
            allow_patterns=allow_patterns,
            approval_enabled=self.exec_config.approval_enabled,
            approval_risk_patterns=self.exec_config.approval_risk_patterns or None,
            risk_assessor=self._assess_exec_risk if self.exec_config.approval_model_enabled else None,
            approval_callback=self._send_exec_approval_request,
            restrict_to_workspace=self.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
        
        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
    
    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or not self._mcp_servers:
            return
        self._mcp_connected = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        self._mcp_stack = AsyncExitStack()
        await self._mcp_stack.__aenter__()
        await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)

    async def _send_exec_approval_request(self, req: dict[str, Any]) -> None:
        """Publish an exec approval request to the original chat."""
        channel = req.get("channel") or "cli"
        chat_id = req.get("chat_id") or "direct"
        command = req.get("command", "")
        request_id = req.get("id", "")
        cwd = req.get("cwd", "")
        risk_text = self._format_exec_risk(req.get("risk_meta"))
        content = "\n".join([
            "### Exec Approval Required",
            "",
            f"- Request: `{request_id}`",
            f"- Workspace: `{cwd}`",
            f"- Command: `{command}`",
            *([f"- Risk: {risk_text}"] if risk_text else []),
            "",
            "**Actions**",
            f"/exec approve {request_id}",
            f"/exec reject {request_id}",
            "/exec pending",
        ])
        metadata: dict[str, Any] = {"format": "markdown", "source": "exec_approval"}
        if channel == "feishu":
            metadata["feishu_interactive_card"] = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "Exec 审核请求"}
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**Request:** `{request_id}`\n"
                            f"**Workspace:** `{cwd}`\n"
                            f"**Command:** `{command}`\n"
                            + (f"**Risk:** {risk_text}" if risk_text else "")
                        ),
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "type": "primary",
                                "text": {"tag": "plain_text", "content": "通过"},
                                "value": {"nanobot_cmd": f"/exec approve {request_id}"},
                            },
                            {
                                "tag": "button",
                                "type": "danger",
                                "text": {"tag": "plain_text", "content": "拒绝"},
                                "value": {"nanobot_cmd": f"/exec reject {request_id}"},
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "查看待审"},
                                "value": {"nanobot_cmd": "/exec pending"},
                            },
                        ],
                    },
                ],
            }
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=content,
                metadata=metadata,
            )
        )

    def _set_tool_context(self, channel: str, chat_id: str, sender_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.set_context(channel, chat_id)

        if spawn_tool := self.tools.get("spawn"):
            if isinstance(spawn_tool, SpawnTool):
                spawn_tool.set_context(channel, chat_id)

        if cron_tool := self.tools.get("cron"):
            if isinstance(cron_tool, CronTool):
                cron_tool.set_context(channel, chat_id)

        if exec_tool := self.tools.get("exec"):
            if isinstance(exec_tool, ExecTool):
                exec_tool.set_context(channel, chat_id, sender_id)

    async def _run_agent_loop(self, initial_messages: list[dict]) -> tuple[str | None, list[str]]:
        """
        Run the agent iteration loop.

        Args:
            initial_messages: Starting messages for the LLM conversation.

        Returns:
            Tuple of (final_content, list_of_tools_used).
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                messages.append({"role": "user", "content": "Reflect on the results and decide next steps."})
            else:
                final_content = response.content
                break

        return final_content, tools_used

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage, session_key: str | None = None) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).
        
        Returns:
            The response message, or None if no response needed.
        """
        # System messages route back via chat_id ("channel:chat_id")
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")

        # Debug log agent inbound (best-effort)
        try:
            from nanobot.utils.helpers import get_data_path
            from datetime import datetime
            import json as _json
            log_path = get_data_path() / "agent_inbound.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "channel": msg.channel,
                "sender_id": msg.sender_id,
                "chat_id": msg.chat_id,
                "preview": preview,
                "content_repr": repr(msg.content),
            }
            with log_path.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass
        
        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        
        # Update tool routing context as early as possible (also needed by direct commands)
        self._set_tool_context(msg.channel, msg.chat_id, msg.sender_id)

        # Handle slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            # Capture messages before clearing (avoid race condition with background task)
            messages_to_archive = session.messages.copy()
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)

            async def _consolidate_and_cleanup():
                temp_session = Session(key=session.key)
                temp_session.messages = messages_to_archive
                await self._consolidate_memory(temp_session, archive_all=True)

            asyncio.create_task(_consolidate_and_cleanup())
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started. Memory consolidation in progress.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="馃悎 nanobot commands:\n/new 鈥?Start a new conversation\n/help 鈥?Show available commands")
        
        # Custom direct commands (session watch / exec)
        if cmd_response := await self._handle_direct_command(msg):
            return cmd_response

        if len(session.messages) > self.memory_window:
            asyncio.create_task(self._consolidate_memory(session))

        initial_messages = self.context.build_messages(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        final_content, tools_used = await self._run_agent_loop(initial_messages)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."
        
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")
        
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content,
                            tools_used=tools_used if tools_used else None)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},  # Pass through for channel-specific needs (e.g. Slack thread_ts)
        )

    async def _handle_direct_command(self, msg: InboundMessage) -> OutboundMessage | None:
        """Handle direct commands without LLM."""
        content = msg.content.strip()
        if not content:
            return None

        # Codex commands
        if self.codex and self.codex.config.enabled:
            prefix = (self.codex.config.command_prefix or "/cx").strip()
            if prefix and content.startswith(prefix):
                if not self._is_sender_allowed(
                    msg.sender_id, self.codex.config.allow_from
                ):
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Access denied for codex commands.",
                    )
                cmdline = content[len(prefix):].strip()
                reply = self._handle_codex_command(cmdline, msg)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=reply,
                    metadata={"format": "markdown", "source": "codex"},
                )

        # Direct exec command
        exec_prefix = (self.exec_config.direct_prefix or "/exec").strip()
        if exec_prefix and content.startswith(exec_prefix):
            command = content[len(exec_prefix):].strip()
            if not command:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Usage: {exec_prefix} <command>",
                )
            exec_tool = self.tools.get("exec")
            if not isinstance(exec_tool, ExecTool):
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Exec tool is not available.",
                )
            try:
                parts = shlex.split(command)
            except ValueError as e:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Invalid command syntax: {e}",
                )
            if parts:
                sub = parts[0].lower()
                if sub == "pending":
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=exec_tool.list_pending_text(chat_id=msg.chat_id),
                        metadata={"format": "markdown", "source": "exec"},
                    )
                if sub in ("approve", "reject"):
                    if len(parts) < 2:
                        return OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=f"Usage: {exec_prefix} {sub} <request_id>",
                        )
                    approved = sub == "approve"
                    result = await exec_tool.resolve_pending(
                        request_id=parts[1],
                        approved=approved,
                        actor_id=msg.sender_id,
                    )
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=result,
                    )
            if not self.exec_config.direct_enabled:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Direct exec disabled: set tools.exec.directEnabled=true.",
                )
            allow_patterns = self.exec_config.direct_allow_patterns or []
            if not allow_patterns:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Direct exec disabled: set tools.exec.directAllowPatterns.",
                )
            if not self._match_any_pattern(command, allow_patterns):
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Command blocked by direct allowlist.",
                )
            result = await self.tools.execute("exec", {"command": command})
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=result,
            )

        return None

    def _handle_codex_command(self, cmdline: str, msg: InboundMessage) -> str:
        if not cmdline or cmdline in ("help", "/help", "?"):
            workspace = self.codex.get_effective_workdir() if self.codex else str(self.workspace)
            return "\n".join([
                "### Codex Command Help",
                "",
                f"**Workspace:** `{workspace}`",
                "",
                "**Common Usage**",
                "- Run once: `/cx run [prompt]`",
                "- Run in directory: `/cx run --cwd [path] [prompt]`",
                "- Continue session: `/cx resume [session_id] [prompt]`",
                "- List sessions: `/cx sessions 10`",
                "- Tail logs: `/cx tail [run_name] 80`",
                "",
                "**Command List**",
                "- `/cx run [prompt] [-- [codex args]]`",
                "- `/cx run -n [name] [prompt] [-- [codex args]]`",
                "- `/cx run --cwd [path] [prompt] [-- [codex args]]`",
                "- `/cx review [prompt] [-- [codex args]]`",
                "- `/cx resume [session_id] [prompt] [-- [codex args]]`",
                "- `/cx apply [task_id] [-- [codex args]]`",
                "- `/cx list`",
                "- `/cx sessions [n]`",
                "- `/cx diff [run_name|session_id|path]`",
                "- `/cx tail <name> [lines]`",
                "- `/cx stop <name>`",
                "- `/cx pending`",
                "- `/cx approve <name>`",
                "- `/cx reject <name>`",
                "- `/cx stream on|off`",
                "- `/cx bind` / `/cx unbind`",
                "- `/cx id`",
            ])

        parts = shlex.split(cmdline)
        if not parts:
            return "No command provided."

        sub = parts[0].lower()
        args = parts[1:]
        extra_args: list[str] = []
        if "--" in args:
            idx = args.index("--")
            extra_args = args[idx + 1 :]
            args = args[:idx]

        try:
            if sub in ("run", "exec"):
                name = None
                run_cwd = None
                consumed: list[str] = []
                i = 0
                while i < len(args):
                    token = args[i]
                    if token in ("-n", "--name"):
                        if i + 1 >= len(args):
                            return "Usage: /cx run [-n name] [--cwd path] [prompt] [-- [codex args]]"
                        name = args[i + 1]
                        i += 2
                        continue
                    if token in ("-C", "--cwd", "--workdir"):
                        if i + 1 >= len(args):
                            return "Usage: /cx run [-n name] [--cwd path] [prompt] [-- [codex args]]"
                        run_cwd = args[i + 1]
                        i += 2
                        continue
                    consumed.append(token)
                    i += 1

                prompt = " ".join(consumed).strip()
                if not prompt:
                    return "Usage: /cx run [-n name] [--cwd path] [prompt] [-- [codex args]]"
                run = self.codex.run_exec(
                    prompt=prompt,
                    name=name,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    work_dir=run_cwd,
                    extra_args=extra_args,
                )
                return "\n".join([
                    "### Started",
                    f"- Run: `{run.name}`",
                    f"- Workspace: `{run.cwd}`",
                    "",
                    "I will send the answer directly when this turn finishes.",
                ])

            if sub == "review":
                name = None
                if len(args) >= 2 and args[0] in ("-n", "--name"):
                    name = args[1]
                    args = args[2:]
                prompt = " ".join(args).strip()
                run = self.codex.run_review(
                    prompt=prompt,
                    name=name,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    extra_args=extra_args,
                )
                return "\n".join([
                    "### Started",
                    f"- Run: `{run.name}`",
                    f"- Workspace: `{run.cwd}`",
                    "",
                    "I will send the answer directly when this turn finishes.",
                ])

            if sub == "resume":
                name = None
                if len(args) >= 2 and args[0] in ("-n", "--name"):
                    name = args[1]
                    args = args[2:]
                session_id = args[0] if args else None
                prompt = " ".join(args[1:]).strip() if args else ""
                run = self.codex.run_resume(
                    session_id=session_id,
                    prompt=prompt,
                    name=name,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    extra_args=extra_args,
                )
                return "\n".join([
                    "### Started",
                    f"- Run: `{run.name}`",
                    f"- Workspace: `{run.cwd}`",
                    f"- Session: `{session_id or 'latest'}`",
                    "",
                    "I will send the answer directly when this turn finishes.",
                ])

            if sub == "apply":
                if not args:
                    return "Usage: /cx apply [task_id] [-- [codex args]]"
                task_id = args[0]
                run = self.codex.run_apply(
                    task_id=task_id,
                    name=None,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    extra_args=extra_args,
                )
                return "\n".join([
                    "### Started",
                    f"- Run: `{run.name}`",
                    f"- Workspace: `{run.cwd}`",
                    "",
                    "I will send the answer directly when this turn finishes.",
                ])

            if sub in ("list", "status"):
                return self.codex.list_runs_text()

            if sub == "sessions":
                limit = 10
                if args and args[0].isdigit():
                    limit = int(args[0])
                return self.codex.list_sessions_text(limit=limit)

            if sub == "diff":
                target = args[0] if args else None
                return self.codex.diff_files_text(target=target)

            if sub == "tail":
                if not args:
                    return "Usage: /cx tail [name] [lines]"
                lines = 40
                if len(args) > 1 and args[1].isdigit():
                    lines = int(args[1])
                return self.codex.tail_run(args[0], lines=lines)

            if sub == "stop":
                if not args:
                    return "Usage: /cx stop [name]"
                return self.codex.stop_run(args[0])

            if sub == "pending":
                return self.codex.list_pending_text()

            if sub == "approve":
                if not args:
                    return "Usage: /cx approve [name]"
                return self.codex.submit_approval(args[0], approved=True)

            if sub == "reject":
                if not args:
                    return "Usage: /cx reject [name]"
                return self.codex.submit_approval(args[0], approved=False)

            if sub == "stream":
                if not args:
                    return "Usage: /cx stream on|off"
                flag = args[0].lower() in ("on", "true", "1", "yes")
                self.codex.set_stream_enabled(flag)
                return f"Streaming {'enabled' if flag else 'disabled'}."

            if sub == "bind":
                self.codex.bind_notify_target(msg.channel, msg.chat_id)
                return "Bound current chat as codex notify target."

            if sub == "unbind":
                self.codex.unbind_notify_target()
                return "Cleared codex notify target."

            if sub == "id":
                return f"chat_id: {msg.chat_id}\nsender_id: {msg.sender_id}"

            return "Unknown command. Send '/cx' to view help."
        except Exception as e:
            return f"Codex error: {e}"

    async def _assess_exec_risk(self, command: str, cwd: str) -> dict[str, Any] | None:
        """Optional model-assisted risk assessment for shell commands."""
        if not self.exec_config.approval_model_enabled:
            return None

        model_name = (self.exec_config.approval_model or self.model).strip() or self.model
        min_level = self._normalize_risk_level(self.exec_config.approval_model_min_level)
        max_tokens = max(64, int(self.exec_config.approval_model_max_tokens or 200))
        temperature = float(self.exec_config.approval_model_temperature)

        prompt = (
            "You are a shell command risk classifier.\n"
            "Classify the command risk level for potential data modification/destruction/"
            "security impact.\n"
            "Return ONLY JSON with keys: level, reason.\n"
            "level must be one of: low, medium, high.\n"
            "Guidance:\n"
            "- Read-only commands (ls/cat/pwd/git status/git log) => low\n"
            "- File writes, installs, config changes => medium or high\n"
            "- Destructive/irreversible/system-impacting commands => high"
        )
        user_payload = json.dumps(
            {"command": command, "working_dir": cwd},
            ensure_ascii=False,
        )

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_payload},
                ],
                tools=None,
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            raw = (response.content or "").strip()
            parsed = self._extract_first_json_object(raw)
            if not isinstance(parsed, dict):
                raise ValueError("risk model did not return valid JSON object")
            level = self._normalize_risk_level(parsed.get("level") or parsed.get("risk"))
            reason = str(parsed.get("reason") or "").strip()
            require = self._risk_level_score(level) >= self._risk_level_score(min_level)
            return {
                "source": "model",
                "level": level,
                "reason": reason or "model classified command risk",
                "require_approval": require,
            }
        except Exception as e:
            require = bool(self.exec_config.approval_model_require_on_error)
            return {
                "source": "model",
                "level": "unknown",
                "reason": f"model risk check failed: {e}",
                "require_approval": require,
            }

    @staticmethod
    def _extract_first_json_object(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            maybe = raw[start : end + 1]
            try:
                obj = json.loads(maybe)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return None
        return None

    @staticmethod
    def _normalize_risk_level(level: Any) -> str:
        lv = str(level or "").strip().lower()
        if lv in {"low", "medium", "high"}:
            return lv
        if lv in {"med", "moderate"}:
            return "medium"
        if lv in {"critical", "severe"}:
            return "high"
        if lv in {"safe", "readonly", "read-only"}:
            return "low"
        return "unknown"

    @staticmethod
    def _risk_level_score(level: str) -> int:
        return {"low": 1, "medium": 2, "high": 3, "unknown": 0}.get(level, 0)

    @staticmethod
    def _format_exec_risk(meta: Any) -> str:
        if not isinstance(meta, dict):
            return ""
        chunks: list[str] = []
        if meta.get("regex_hit"):
            chunks.append("regex 命中")
        model = meta.get("model")
        if isinstance(model, dict):
            level = str(model.get("level") or "").strip()
            reason = str(model.get("reason") or "").strip()
            if level:
                if reason:
                    if len(reason) > 120:
                        reason = reason[:120] + "..."
                    chunks.append(f"model={level} ({reason})")
                else:
                    chunks.append(f"model={level}")
        return "; ".join(chunks)

    @staticmethod
    def _match_any_pattern(text: str, patterns: list[str]) -> bool:
        lower = text.lower()
        for pat in patterns:
            try:
                if re.search(pat, lower):
                    return True
            except re.error:
                if pat.lower() in lower:
                    return True
        return False

    @staticmethod
    def _is_sender_allowed(sender_id: str, allow_list: list[str]) -> bool:
        if not allow_list:
            return True
        sender_str = str(sender_id)
        if sender_str in allow_list:
            return True
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        self._set_tool_context(origin_channel, origin_chat_id)
        initial_messages = self.context.build_messages(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        final_content, _ = await self._run_agent_loop(initial_messages)

        if final_content is None:
            final_content = "Background task completed."
        
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md.

        Args:
            archive_all: If True, clear all messages and reset session (for /new command).
                       If False, only write to files without modifying session.
        """
        memory = MemoryStore(self.workspace)

        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info(f"Memory consolidation (archive_all): {len(session.messages)} total messages archived")
        else:
            keep_count = self.memory_window // 2
            if len(session.messages) <= keep_count:
                logger.debug(f"Session {session.key}: No consolidation needed (messages={len(session.messages)}, keep={keep_count})")
                return

            messages_to_process = len(session.messages) - session.last_consolidated
            if messages_to_process <= 0:
                logger.debug(f"Session {session.key}: No new messages to consolidate (last_consolidated={session.last_consolidated}, total={len(session.messages)})")
                return

            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return
            logger.info(f"Memory consolidation started: {len(session.messages)} total, {len(old_messages)} new to consolidate, {keep_count} keep")

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")
        conversation = "\n".join(lines)
        current_memory = memory.read_long_term()

        prompt = f"""You are a memory consolidation agent. Process this conversation and return a JSON object with exactly two keys:

1. "history_entry": A paragraph (2-5 sentences) summarizing the key events/decisions/topics. Start with a timestamp like [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.

2. "memory_update": The updated long-term memory content. Add any new facts: user location, preferences, personal info, habits, project context, technical decisions, tools/services used. If nothing new, return the existing content unchanged.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}

Respond with ONLY valid JSON, no markdown fences."""

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Respond only with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
            )
            text = (response.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json.loads(text)

            if entry := result.get("history_entry"):
                memory.append_history(entry)
            if update := result.get("memory_update"):
                if update != current_memory:
                    memory.write_long_term(update)

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = len(session.messages) - keep_count
            logger.info(f"Memory consolidation done: {len(session.messages)} messages, last_consolidated={session.last_consolidated}")
        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}")

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).
        
        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).
            channel: Source channel (for tool context routing).
            chat_id: Source chat ID (for tool context routing).
        
        Returns:
            The agent's response.
        """
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg, session_key=session_key)
        return response.content if response else ""

