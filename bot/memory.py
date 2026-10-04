"""ความจำบทสนทนาแยกตามช่อง

เก็บลงฐานข้อมูล SQLite (ไม่หายเมื่อรีสตาร์ท) หรือเก็บใน RAM อย่างเดียวถ้าตั้ง MEMORY_PERSIST=false
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .storage import Database

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


class ChannelMemory:
    def __init__(self, max_messages: int = 10, db: Database | None = None) -> None:
        self._max = max_messages
        self._db = db
        self._history: dict[int, deque[ChatMessage]] = defaultdict(
            lambda: deque(maxlen=self._max)
        )
        # ล็อกต่อช่อง: กันไม่ให้คำถามหลายอันในช่องเดียวกันแทรกกันจนประวัติสลับลำดับ
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, channel_id: int) -> asyncio.Lock:
        return self._locks[channel_id]

    def _load(self, channel_id: int) -> list[tuple[int | None, ChatMessage]]:
        """คืน [(id ในฐานข้อมูล, ข้อความ)] เรียงจากเก่าไปใหม่"""
        if self._db is None:
            return [(None, m) for m in self._history.get(channel_id, ())]
        rows = self._db.conn.execute(
            "SELECT id, role, content FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, self._max),
        ).fetchall()
        return [(row_id, ChatMessage(role, content)) for row_id, role, content in reversed(rows)]

    def get(self, channel_id: int) -> list[ChatMessage]:
        history = [m for _, m in self._load(channel_id)]
        # ถ้าข้อความเก่าถูกตัดออกจนข้อความแรกเป็นของบอท ให้ข้ามไป
        # (API บางเจ้า เช่น Gemini ต้องการให้เริ่มด้วยข้อความของผู้ใช้)
        while history and history[0].role == "assistant":
            history.pop(0)
        return history

    def add_exchange(self, channel_id: int, user_text: str, bot_text: str) -> None:
        """บันทึกคำถาม+คำตอบพร้อมกัน เฉพาะเมื่อ AI ตอบสำเร็จ เพื่อให้ประวัติสลับ user/assistant เสมอ"""
        if self._db is None:
            history = self._history[channel_id]
            history.append(ChatMessage("user", user_text))
            history.append(ChatMessage("assistant", bot_text))
            return
        now = time.time()
        conn = self._db.conn
        conn.executemany(
            "INSERT INTO messages (channel_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(channel_id, "user", user_text, now), (channel_id, "assistant", bot_text, now)],
        )
        # เก็บไว้แค่ MEMORY_SIZE ข้อความล่าสุดของช่อง
        conn.execute(
            "DELETE FROM messages WHERE channel_id = ? AND id NOT IN ("
            " SELECT id FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?)",
            (channel_id, channel_id, self._max),
        )
        conn.commit()

    def remove_exchange(self, channel_id: int, user_text: str, bot_text: str) -> bool:
        """ลบคู่คำถาม+คำตอบที่ระบุออก (ใช้ตอนกด 🔄 ตอบใหม่ หรือ 🗑️ ลบ) คืน True ถ้าเจอ"""
        items = self._load(channel_id)
        for i in range(len(items) - 1):
            if items[i][1] == ChatMessage("user", user_text) and items[i + 1][1] == ChatMessage(
                "assistant", bot_text
            ):
                if self._db is None:
                    history = self._history[channel_id]
                    kept = [m for j, (_, m) in enumerate(items) if j not in (i, i + 1)]
                    history.clear()
                    history.extend(kept)
                else:
                    self._db.conn.executemany(
                        "DELETE FROM messages WHERE id = ?", [(items[i][0],), (items[i + 1][0],)]
                    )
                    self._db.conn.commit()
                return True
        return False

    def reset(self, channel_id: int) -> None:
        self._history.pop(channel_id, None)
        if self._db is not None:
            self._db.conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
            self._db.conn.commit()
