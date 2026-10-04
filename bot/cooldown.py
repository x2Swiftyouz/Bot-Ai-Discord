"""cooldown ต่อผู้ใช้ ใช้ร่วมกันทั้งการ mention และ /ask เพื่อไม่ให้เกินโควต้าฟรี"""

from __future__ import annotations

import time


class UserCooldown:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._last_used: dict[int, float] = {}

    def check(self, user_id: int) -> float:
        """คืนค่าวินาทีที่ยังต้องรอ (0 = ใช้ได้ และเริ่มนับ cooldown ใหม่)"""
        if self._seconds <= 0:
            return 0.0
        now = time.monotonic()
        last = self._last_used.get(user_id)
        if last is not None and (remaining := self._seconds - (now - last)) > 0:
            return remaining
        self._last_used[user_id] = now
        if len(self._last_used) > 10_000:
            self._prune(now)
        return 0.0

    def _prune(self, now: float) -> None:
        self._last_used = {
            uid: t for uid, t in self._last_used.items() if now - t < self._seconds
        }
