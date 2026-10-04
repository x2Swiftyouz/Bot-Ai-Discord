"""ความจำบทสนทนาแยกตามช่อง เก็บในหน่วยความจำ (หายเมื่อรีสตาร์ทบอท)"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


class ChannelMemory:
    def __init__(self, max_messages: int = 10) -> None:
        self._max = max_messages
        self._history: dict[int, deque[ChatMessage]] = defaultdict(
            lambda: deque(maxlen=self._max)
        )
        # ล็อกต่อช่อง: กันไม่ให้คำถามหลายอันในช่องเดียวกันแทรกกันจนประวัติสลับลำดับ
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, channel_id: int) -> asyncio.Lock:
        return self._locks[channel_id]

    def get(self, channel_id: int) -> list[ChatMessage]:
        history = list(self._history.get(channel_id, ()))
        # ถ้าข้อความเก่าถูกตัดออกจนข้อความแรกเป็นของบอท ให้ข้ามไป
        # (API บางเจ้า เช่น Gemini ต้องการให้เริ่มด้วยข้อความของผู้ใช้)
        while history and history[0].role == "assistant":
            history.pop(0)
        return history

    def add_exchange(self, channel_id: int, user_text: str, bot_text: str) -> None:
        """บันทึกคำถาม+คำตอบพร้อมกัน เฉพาะเมื่อ AI ตอบสำเร็จ เพื่อให้ประวัติสลับ user/assistant เสมอ"""
        history = self._history[channel_id]
        history.append(ChatMessage("user", user_text))
        history.append(ChatMessage("assistant", bot_text))

    def reset(self, channel_id: int) -> None:
        self._history.pop(channel_id, None)
