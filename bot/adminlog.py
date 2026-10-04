"""ห้อง log ของแอดมิน: แจ้งเหตุการณ์สำคัญของบอทเข้าห้อง Discord ที่ตั้งไว้

ตั้งห้องด้วยคำสั่ง /logchannel หรือ LOG_CHANNEL_ID ใน .env
เหตุการณ์ซ้ำ ๆ (เช่น สลับไปตัวสำรอง) แจ้งไม่เกินครั้งละ COOLDOWN วินาทีต่อเรื่อง กันห้องรก
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from .utils import redact

if TYPE_CHECKING:
    from main import AIChatBot

log = logging.getLogger(__name__)

SETTING_KEY = "log_channel_id"

STATUS_COLORS = {
    "down": discord.Color.orange(),
    "outage": discord.Color.red(),
    "up": discord.Color.green(),
    "recovered": discord.Color.green(),
    "info": discord.Color.blurple(),
    "warning": discord.Color.gold(),
}


class AdminLog:
    COOLDOWN = 600

    def __init__(self, bot: AIChatBot, default_channel_id: int | None) -> None:
        self.bot = bot
        self.default_channel_id = default_channel_id
        self._last_sent: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def channel_id(self) -> int | None:
        saved = self.bot.db.get_setting(SETTING_KEY)
        if saved == "off":
            return None
        return int(saved) if saved else self.default_channel_id

    def set_channel(self, channel_id: int | None) -> None:
        self.bot.db.set_setting(SETTING_KEY, str(channel_id) if channel_id else "off")

    def status(self, kind: str, title: str, detail: str = "") -> None:
        """การ์ดสีแจ้งสถานะ (ส้ม = ใช้ไม่ได้, แดง = ใช้ไม่ได้ทุกตัว, เขียว = กลับมาแล้ว)"""
        embed = discord.Embed(
            title=title, description=detail or None,
            color=STATUS_COLORS.get(kind, discord.Color.blurple()),
            timestamp=discord.utils.utcnow(),
        )
        self.post(embed=embed)

    def post(
        self,
        message: str | None = None,
        *,
        embed: discord.Embed | None = None,
        key: str | None = None,
        cooldown: float = COOLDOWN,
    ) -> None:
        """ส่งข้อความเข้าห้อง log (ไม่ block) — key เดิมภายใน cooldown จะถูกข้าม"""
        if self.channel_id is None:
            return
        if key is not None:
            now = time.monotonic()
            if now - self._last_sent.get(key, -cooldown) < cooldown:
                return
            self._last_sent[key] = now
        task = asyncio.get_running_loop().create_task(self._send(message, embed))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, message: str | None, embed: discord.Embed | None) -> None:
        channel_id = self.channel_id
        if channel_id is None:
            return
        channel = self.bot.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            content = redact(message)[:2000] if message else None
            await channel.send(content, embed=embed)  # type: ignore[union-attr]
        except (discord.HTTPException, AttributeError) as e:
            log.warning("ส่งข้อความเข้าห้อง log (%s) ไม่ได้: %r", channel_id, e)
