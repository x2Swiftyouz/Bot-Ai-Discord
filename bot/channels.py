"""ห้องคุยกับ AI: ในห้องเหล่านี้บอทจะตอบทุกข้อความ ไม่ต้องพิมพ์ /ask หรือ mention

ห้องมาจาก 2 แหล่ง:
- AI_CHANNEL_IDS ใน .env (ตั้งตายตัว ปิดด้วยคำสั่งไม่ได้)
- คำสั่ง /aichannel ใน Discord (บันทึกลงไฟล์ JSON จึงยังอยู่หลังรีสตาร์ท)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class AIChannelStore:
    def __init__(self, fixed_ids: tuple[int, ...], path: Path) -> None:
        self._fixed = set(fixed_ids)
        self._path = path
        self._dynamic: set[int] = self._load()

    def _load(self) -> set[int]:
        try:
            return {int(x) for x in json.loads(self._path.read_text(encoding="utf-8"))}
        except FileNotFoundError:
            return set()
        except (ValueError, TypeError, OSError) as e:
            log.warning("อ่านไฟล์ %s ไม่ได้ (%r) เริ่มใหม่แบบว่าง", self._path, e)
            return set()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self._dynamic)), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            log.exception("บันทึกไฟล์ %s ไม่ได้ (การตั้งค่าจะหายเมื่อรีสตาร์ท)", self._path)

    def is_ai_channel(self, channel_id: int) -> bool:
        return channel_id in self._fixed or channel_id in self._dynamic

    def is_fixed(self, channel_id: int) -> bool:
        return channel_id in self._fixed

    def enable(self, channel_id: int) -> None:
        self._dynamic.add(channel_id)
        self._save()

    def disable(self, channel_id: int) -> None:
        self._dynamic.discard(channel_id)
        self._save()

    def all_ids(self) -> set[int]:
        return self._fixed | self._dynamic
