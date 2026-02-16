"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import re
import threading
from datetime import datetime
from collections import OrderedDict
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig
from nanobot.utils.helpers import get_data_path

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        P2ImMessageReceiveV1,
        P2ImChatAccessEventBotP2pChatEnteredV1,
        P2ImMessageReactionCreatedV1,
        P2ImMessageReactionDeletedV1,
        P2ImMessageMessageReadV1,
    )
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.
    
    Supports two formats:
    1. Direct format: {"title": "...", "content": [...]}
    2. Localized format: {"zh_cn": {"title": "...", "content": [...]}}
    """
    def extract_from_lang(lang_content: dict) -> str | None:
        if not isinstance(lang_content, dict):
            return None
        title = lang_content.get("title", "")
        content_blocks = lang_content.get("content", [])
        if not isinstance(content_blocks, list):
            return None
        text_parts = []
        if title:
            text_parts.append(title)
        for block in content_blocks:
            if not isinstance(block, list):
                continue
            for element in block:
                if isinstance(element, dict):
                    tag = element.get("tag")
                    if tag == "text":
                        text_parts.append(element.get("text", ""))
                    elif tag == "a":
                        text_parts.append(element.get("text", ""))
                    elif tag == "at":
                        text_parts.append(f"@{element.get('user_name', 'user')}")
        return " ".join(text_parts).strip() if text_parts else None
    
    # Try direct format first
    if "content" in content_json:
        result = extract_from_lang(content_json)
        if result:
            return result
    
    # Try localized format
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang_content = content_json.get(lang_key)
        result = extract_from_lang(lang_content)
        if result:
            return result
    
    return ""


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.
    
    Uses WebSocket to receive events - no public IP or webhook required.
    
    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """
    
    name = "feishu"
    
    def __init__(self, config: FeishuConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
    
    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).register_p2_card_action_trigger(
            self._on_card_action_sync
        ).register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
            self._on_chat_entered_sync
        ).register_p2_im_message_reaction_created_v1(
            self._on_message_reaction_created_sync
        ).register_p2_im_message_reaction_deleted_v1(
            self._on_message_reaction_deleted_sync
        ).register_p2_im_message_message_read_v1(
            self._on_message_read_sync
        ).build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning(f"Feishu WebSocket error: {e}")
                if self._running:
                    import time; time.sleep(5)
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).
        
        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            level = len(m.group(1))
            text = m.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _flatten_form_text(value: Any) -> str:
        """Best-effort flatten Feishu card form values into a plain string."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, dict):
            for key in ("value", "text", "input", "content"):
                if key in value:
                    got = FeishuChannel._flatten_form_text(value.get(key))
                    if got:
                        return got
            parts = [FeishuChannel._flatten_form_text(v) for v in value.values()]
            return " ".join([p for p in parts if p]).strip()
        if isinstance(value, list):
            parts = [FeishuChannel._flatten_form_text(v) for v in value]
            return " ".join([p for p in parts if p]).strip()
        return ""

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return
        
        # Debug log outbound (best-effort)
        try:
            log_path = get_data_path() / "feishu_outbound.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            preview = msg.content[:200] + ("..." if len(msg.content) > 200 else "")
            payload = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "chat_id": msg.chat_id,
                "channel": msg.channel,
                "preview": preview,
                "metadata": msg.metadata or {},
            }
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

        try:
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"

            custom_card = (msg.metadata or {}).get("feishu_interactive_card")
            fmt = (msg.metadata or {}).get("format")
            if custom_card:
                content = json.dumps(custom_card, ensure_ascii=False)
                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(msg.chat_id)
                        .msg_type("interactive")
                        .content(content)
                        .build()
                    ).build()
                response = self._client.im.v1.message.create(request)
                if not response.success():
                    logger.error(
                        f"Failed to send Feishu interactive card: code={response.code}, "
                        f"msg={response.msg}, log_id={response.get_log_id()}"
                    )
                return

            if fmt == "text":
                content = json.dumps({"text": msg.content}, ensure_ascii=False)
                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(msg.chat_id)
                        .msg_type("text")
                        .content(content)
                        .build()
                    ).build()
                response = self._client.im.v1.message.create(request)
                if not response.success():
                    logger.error(
                        f"Failed to send Feishu message: code={response.code}, "
                        f"msg={response.msg}, log_id={response.get_log_id()}"
                    )
                else:
                    logger.debug(f"Feishu message sent to {msg.chat_id}")
                return
            
            # Build card with markdown + table support
            elements = self._build_card_elements(msg.content)
            card = {
                "config": {"wide_screen_mode": True},
                "elements": elements,
            }
            content = json.dumps(card, ensure_ascii=False)
            
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(msg.chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                ).build()
            
            response = self._client.im.v1.message.create(request)
            
            if not response.success():
                logger.error(
                    f"Failed to send Feishu message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
            else:
                logger.debug(f"Feishu message sent to {msg.chat_id}")
                
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    def _on_chat_entered_sync(self, data: "P2ImChatAccessEventBotP2pChatEnteredV1") -> None:
        """Sync handler for bot_p2p_chat_entered events (no-op, prevents SDK error)."""
        try:
            event = getattr(data, "event", None)
            user_id = None
            if event and getattr(event, "operator_id", None):
                user_id = event.operator_id.open_id
            logger.info(f"Feishu event: bot_p2p_chat_entered (operator={user_id or 'unknown'})")
        except Exception:
            logger.info("Feishu event: bot_p2p_chat_entered")

    def _on_message_reaction_created_sync(self, data: "P2ImMessageReactionCreatedV1") -> None:
        """Sync handler for message reaction created (no-op)."""
        logger.info("Feishu event: message_reaction_created")

    def _on_message_reaction_deleted_sync(self, data: "P2ImMessageReactionDeletedV1") -> None:
        """Sync handler for message reaction deleted (no-op)."""
        logger.info("Feishu event: message_reaction_deleted")

    def _on_message_read_sync(self, data: "P2ImMessageMessageReadV1") -> None:
        """Sync handler for message read (no-op)."""
        logger.info("Feishu event: message_read")

    def _on_card_action_sync(self, data: "P2CardActionTrigger") -> "P2CardActionTriggerResponse":
        """Sync handler for Feishu interactive card actions."""
        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._on_card_action(data), self._loop)
        except Exception as e:
            logger.warning(f"Feishu card action dispatch failed: {e}")
        return P2CardActionTriggerResponse({
            "toast": {"type": "info", "content": "Received. Processing..."},
        })

    async def _on_card_action(self, data: "P2CardActionTrigger") -> None:
        """Handle Feishu interactive card button clicks and convert them to bot commands."""
        try:
            event = getattr(data, "event", None)
            if not event:
                return
            action = getattr(event, "action", None)
            value = getattr(action, "value", None) if action else None
            if not isinstance(value, dict):
                return
            cmd = (value.get("nanobot_cmd") or "").strip()
            action_name = (value.get("nanobot_action") or "").strip()
            if not cmd and action_name == "cx_quick_continue":
                sid = (value.get("session_id") or "").strip()
                form_value = getattr(action, "form_value", None) if action else None
                prompt = ""
                if isinstance(form_value, dict):
                    # Some Feishu payloads are flat: {"resume_prompt": "..."}
                    prompt = self._flatten_form_text(form_value.get("resume_prompt")).strip()
                    # Some are nested by form name: {"resume_form": {"resume_prompt": "..."}}
                    if not prompt:
                        nested = form_value.get("resume_form")
                        if isinstance(nested, dict):
                            prompt = self._flatten_form_text(nested.get("resume_prompt")).strip()
                    # Fallback: flatten all keys for compatibility.
                    if not prompt:
                        prompt = self._flatten_form_text(form_value).strip()
                if not prompt:
                    prompt = self._flatten_form_text(getattr(action, "input_value", None)).strip()
                if sid:
                    cmd = f"/cx resume {sid}"
                    if prompt:
                        cmd += f" {prompt}"
                else:
                    cmd = "/cx sessions 5"
            if not cmd:
                return

            operator = getattr(event, "operator", None)
            sender_id = (
                (getattr(operator, "open_id", None) or "")
                or (getattr(operator, "user_id", None) or "")
                or "unknown"
            )
            context = getattr(event, "context", None)
            chat_id = (
                (getattr(context, "open_chat_id", None) or "")
                or sender_id
            )

            logger.info(f"Feishu card action: sender={sender_id} chat_id={chat_id} cmd={cmd}")
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=cmd,
                metadata={
                    "source": "feishu_card_action",
                    "card_action": value,
                    "message_id": getattr(context, "open_message_id", None) if context else None,
                },
            )
        except Exception as e:
            logger.error(f"Error processing Feishu card action: {e}")
    
    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender
            
            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            
            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)
            
            # Skip bot messages
            sender_type = sender.sender_type
            if sender_type == "bot":
                return
            
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type
            
            # No auto-reaction
            
            # Parse message content
            if msg_type == "text":
                try:
                    content = json.loads(message.content).get("text", "")
                except json.JSONDecodeError:
                    content = message.content or ""
            elif msg_type == "post":
                try:
                    content_json = json.loads(message.content)
                    content = _extract_post_text(content_json)
                except (json.JSONDecodeError, TypeError):
                    content = message.content or ""
            else:
                content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
            
            # Log inbound message payload for debugging
            try:
                log_path = get_data_path() / "feishu_inbound.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "event": "im.message.receive_v1",
                    "message_id": message_id,
                    "chat_id": message.chat_id,
                    "chat_type": chat_type,
                    "sender_id": sender_id,
                    "msg_type": msg_type,
                    "content": content,
                    "content_repr": repr(content),
                    "raw_content": message.content,
                }
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception:
                pass

            if not content:
                return

            logger.info(
                f"Feishu inbound: sender={sender_id} chat_id={message.chat_id} "
                f"chat_type={chat_type} msg_type={msg_type}"
            )
            
            # Forward to message bus (reply to chat_id for both group and p2p)
            reply_to = chat_id or sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                }
            )
            
        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")
