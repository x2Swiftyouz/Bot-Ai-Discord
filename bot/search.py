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
    # เกม / รีวิว / สถานที่ — ข้อมูลเปลี่ยนบ่อย หรือ AI มักเดาผิด
    r"|ตั้งค่า|สเปค|สเปก|แพทช์|ซีซั่น|เมต้า|โค้ดรีดีม|รีดีม|รีวิว|ร้าน.{0,10}(แถว|ใกล้)|ที่เที่ยว"
    # คลิป / วิดีโอ — ค้นแล้วส่งลิงก์ให้
    r"|คลิป|วิดีโอ|วีดีโอ|ยูทูป|ยูทูบ|ติ๊กต็อก"
    r"|\b(news|latest|price|weather|forecast|score|release[ds]?|update[ds]?|patch|season|meta"
    r"|settings?|specs?|review|video|youtube|tiktok|redeem)\b",
    re.IGNORECASE,
)
# คำถามเรื่องวันเวลาตอบได้จากนาฬิกาของบอทอยู่แล้ว ไม่ต้องค้นเว็บ
_DATE_ONLY = re.compile(
    r"^(วันนี้|ตอนนี้|now|today)\s*(คือ|เป็น)?\s*(วันอะไร|วันที่เท่าไหร่|วันที่เท่าไร|กี่โมง|เวลาเท่าไหร่"
    r"|เดือนอะไร|ปีอะไร|what day|what time|what date)",
    re.IGNORECASE,
)
_FORCE_PREFIX = re.compile(r"^\s*(ค้นหา|ค้นเว็บ|search)\s*[:：]?\s*", re.IGNORECASE)
# ขึ้นต้นด้วย "หา..." / "ช่วยหา..." = ขอให้ไปหาข้อมูล (ไม่นับ "หาร" "หาย")
_FIND_PREFIX = re.compile(r"^\s*(ช่วย|รบกวน)?\s*(หา|ค้น)(?![รย])", re.IGNORECASE)
_VIDEO = re.compile(r"คลิป|วิดีโอ|วีดีโอ|ยูทูป|ยูทูบ|ติ๊กต็อก|\b(video|youtube|tiktok)\b", re.IGNORECASE)
_TIKTOK = re.compile(r"ติ๊กต็อก|ติกต็อก|\btiktok\b", re.IGNORECASE)
MIN_VIDEO_RESULTS = 2
# คำถามสั้น ๆ อย่าง "หาคลิปให้หน่อย" ต้องเอาหัวข้อจากคำถามก่อนหน้ามาค้นด้วย
SHORT_QUERY = 25
_NEWS = re.compile(r"ข่าว|\bnews\b", re.IGNORECASE)


def is_video_query(question: str) -> bool:
    return bool(_VIDEO.search(question))


def should_search(question: str) -> bool:
    text = question.strip()
    if _FORCE_PREFIX.match(text) or _FIND_PREFIX.match(text):
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

    async def _request(self, query: str, domains: list[str] | None = None) -> list[SearchResult]:
        payload: dict = {
            "query": query,
            "max_results": self.max_results,
            "search_depth": "basic",
            "topic": "news" if _NEWS.search(query) else "general",
        }
        if domains:
            payload["include_domains"] = domains
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
        return [
            SearchResult(r.get("title") or r.get("url", ""), r["url"], (r.get("content") or "")[:700])
            for r in data.get("results") or []
            if r.get("url")
        ]

    async def search(self, question: str, context: str = "") -> list[SearchResult]:
        """context: คำถามก่อนหน้าในห้อง ใช้เติมหัวข้อเมื่อคำถามนี้สั้นและกว้างเกินไป"""
        query = _FORCE_PREFIX.sub("", question).strip()
        if context and len(query) < SHORT_QUERY:
            query = f"{context.strip()[:300]} {query}"
        query = query[:400]
        if not _VIDEO.search(query):
            results = await self._request(query)
        elif _TIKTOK.search(query):
            results = await self._request(query, ["tiktok.com"])
        else:
            # ขอคลิป → YouTube ก่อน (ตรงหัวข้อกว่า) ถ้าได้น้อยเกินค่อยเติมจาก TikTok
            results = await self._request(query, ["youtube.com"])
            if len(results) < MIN_VIDEO_RESULTS:
                seen = {r.url for r in results}
                results += [r for r in await self._request(query, ["tiktok.com"]) if r.url not in seen]
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
