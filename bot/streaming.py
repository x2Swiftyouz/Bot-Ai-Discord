"""แสดงคำตอบค่อย ๆ พิมพ์ออกมาระหว่างที่ AI กำลังตอบ (แก้ข้อความเดิมเป็นระยะ)

Discord จำกัดการแก้ข้อความ (~5 ครั้ง / 5 วินาที ต่อช่อง) จึงอัปเดตทุก ๆ INTERVAL วินาที
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from .utils import tables_to_lists

log = logging.getLogger(__name__)

CURSOR = " ▌"
# ข้อความระหว่าง stream แสดงแค่ส่วนต้น (ข้อความเดียว) ตอนจบค่อยตัดเป็นหลายข้อความตามปกติ
PREVIEW_LIMIT = 1900

Message = discord.Message | discord.WebhookMessage


class StreamPreview:
    INTERVAL = 1.2

    def __init__(self, start: Callable[[str], Awaitable[Message]], header: str = "") -> None:
        self._start = start  # ส่งข้อความแรก (reply / followup) แล้วคืนข้อความนั้น
        self._header = header
        self._text = ""
        self._shown = ""
        self._done = False
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.message: Message | None = None

    def update(self, text: str) -> None:
        """เรียกจาก AI ทุกครั้งที่ได้ข้อความเพิ่ม (ไม่ block)"""
        self._text = text
        if self._task is None and not self._done:
            self._task = asyncio.create_task(self._loop())

    def _render(self) -> str:
        body = self._header + tables_to_lists(self._text)
        if len(body) > PREVIEW_LIMIT:
            return body[:PREVIEW_LIMIT] + " …" + CURSOR
        return body + CURSOR

    async def _loop(self) -> None:
        while not self._done:
            if self._text != self._shown:
                self._shown = self._text
                content = self._render()
                try:
                    if self.message is None:
                        self.message = await self._start(content)
                    else:
                        await self.message.edit(content=content)
                except discord.HTTPException as e:
                    log.warning("Stream preview update failed: %r", e)
            try:
                await asyncio.wait_for(self._wake.wait(), self.INTERVAL)
            except TimeoutError:
                pass

    async def finish(self) -> Message | None:
        """หยุดอัปเดต แล้วคืนข้อความ preview (ถ้ามี) ให้ผู้เรียกแก้เป็นคำตอบสุดท้าย"""
        self._done = True
        self._wake.set()
        # ไม่ cancel เพราะถ้ากำลังส่งข้อความแรกอยู่ จะได้ข้อความซ้ำ 2 อัน — รอให้รอบปัจจุบันจบแทน
        if self._task is not None:
            await self._task
        return self.message
