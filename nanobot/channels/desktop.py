"""Desktop virtual channel for local UI communication."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DesktopChannelConfig


class DesktopChannel(BaseChannel):
    """Virtual channel that bridges local desktop UI and nanobot bus."""

    name = "desktop"

    def __init__(self, config: DesktopChannelConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DesktopChannelConfig = config
        self._stop_event = asyncio.Event()
        self._outbound_handler: Callable[[OutboundMessage], Awaitable[None]] | None = None

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        logger.info("Desktop channel started")
        await self._stop_event.wait()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        logger.info("Desktop channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if self._outbound_handler:
            await self._outbound_handler(msg)
            return
        logger.debug("Desktop channel outbound dropped (no consumer)")

    def set_outbound_handler(self, handler: Callable[[OutboundMessage], Awaitable[None]] | None) -> None:
        self._outbound_handler = handler

    async def publish_from_ui(
        self,
        content: str,
        chat_id: str | None = None,
        sender_id: str = "desktop-user",
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        target_chat = (chat_id or self.config.default_chat_id or "desktop-ui").strip() or "desktop-ui"
        await self._handle_message(
            sender_id=sender_id,
            chat_id=target_chat,
            content=content,
            media=media or [],
            metadata=metadata or {},
        )
