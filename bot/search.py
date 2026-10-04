"""ค้นเว็บผ่าน Tavily (ฟรีเดือนละ 1,000 ครั้ง ไม่ต้องผูกบัตร: https://app.tavily.com)

บอทจะค้นเว็บเองเมื่อคำถามดูเหมือนต้องใช้ข้อมูลล่าสุด (ข่าว ราคา ผลกีฬา ฯลฯ)
หรือเมื่อผู้ใช้ขึ้นต้นด้วย "ค้นหา" / "search" แล้วส่งผลให้ AI สรุปพร้อมแหล่งที่มา
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

# คำที่บอกว่าน่าจะต้องใช้ข้อมูลล่าสุด
_NEEDS_SEARCH = re.compile(
    # ไม่ใส่คำกว้าง ๆ อย่าง "วันนี้" "ตอนนี้" เพราะจะค้นเว็บโดยไม่จำเป็น (เช่น "ตอนนี้เหนื่อยจัง")
    r"ข่าว|ล่าสุด|อัปเดต|อัพเดท|สถานการณ์"
    r"|ราคา|ค่าเงิน|อัตราแลกเปลี่ยน|หุ้น|คริปโต|บิทคอยน์|ทองคำ|ราคาทอง|น้ำมัน"
    r"|ผลบอล|ผลการแข่ง|ตารางคะแนน|สภาพอากาศ|พยากรณ์อากาศ|พายุ|แผ่นดินไหว|น้ำท่วม"
    r"|เปิดตัว|วางขาย|เวอร์ชันใหม่|เวอร์ชั่นใหม่|อีเวนต์|คอนเสิร์ต"
    r"|\b(news|latest|price|weather|forecast|score|release[ds]?|update[ds]?)\b",
    re.IGNORECASE,
)
# คำถามเรื่องวันเวลาตอบได้จากนาฬิกาของบอทอยู่แล้ว ไม่ต้องค้นเว็บ
_DATE_ONLY = re.compile(
    r"^(วันนี้|ตอนนี้|now|today)\s*(คือ|เป็น)?\s*(วันอะไร|วันที่เท่าไหร่|วันที่เท่าไร|กี่โมง|เวลาเท่าไหร่"
    r"|เดือนอะไร|ปีอะไร|what day|what time|what date)",
    re.IGNORECASE,
)
_FORCE_PREFIX = re.compile(r"^\s*(ค้นหา|ค้นเว็บ|search)\s*[:：]?\s*", re.IGNORECASE)
_NEWS = re.compile(r"ข่าว|\bnews\b", re.IGNORECASE)


def should_search(question: str) -> bool:
    text = question.strip()
    if _FORCE_PREFIX.match(text):
        return True
    if _DATE_ONLY.match(text):
        return False
    return bool(_NEEDS_SEARCH.search(text))


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    content: str


class SearchError(Exception):
    pass


class TavilySearch:
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, max_results: int = 5, timeout: float = 15) -> None:
        self.api_key = api_key
        self.max_results = max_results
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search(self, question: str) -> list[SearchResult]:
        query = _FORCE_PREFIX.sub("", question).strip()[:400]
        payload = {
            "query": query,
            "max_results": self.max_results,
            "search_depth": "basic",
            "topic": "news" if _NEWS.search(query) else "general",
        }
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with self._session.post(
                self.URL, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}
            ) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:300]
                    raise SearchError(f"Tavily HTTP {resp.status}: {body}")
                data = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as e:
            raise SearchError(f"Tavily connection error: {e!r}") from e

        results = [
            SearchResult(r.get("title") or r.get("url", ""), r["url"], (r.get("content") or "")[:700])
            for r in data.get("results") or []
            if r.get("url")
        ]
        log.info("ค้นเว็บ (Tavily): %r → %d ผลลัพธ์", query, len(results))
        return results


def format_results(results: list[SearchResult], now: str) -> str:
    """แปลงผลค้นเว็บเป็นข้อความแนบท้ายคำถามให้ AI ใช้ตอบ"""
    lines = [
        f"[ผลการค้นเว็บ ณ {now} — ใช้ข้อมูลนี้ตอบให้ตรงและเป็นปัจจุบัน ถ้าไม่เกี่ยวข้องให้ตอบตามความรู้ "
        "ไม่ต้องใส่ลิงก์ในคำตอบ ระบบจะแสดงแหล่งที่มาให้เอง]"
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title} ({r.url})\n{r.content}")
    return "\n\n".join(lines)
