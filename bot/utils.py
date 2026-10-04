"""ฟังก์ชันช่วยเหลือ: ตัดข้อความยาวให้ไม่เกินข้อจำกัด 2000 ตัวอักษรของ Discord"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DISCORD_LIMIT = 2000

_THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
_THAI_MONTHS = [
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
]


def now_text(timezone: str, now: datetime | None = None) -> str:
    """วันเวลาปัจจุบันเป็นภาษาไทย เช่น "วันอาทิตย์ที่ 4 ตุลาคม ค.ศ. 2026 (พ.ศ. 2569) เวลา 08:05 น. (Asia/Bangkok)" """
    now = now or datetime.now(ZoneInfo(timezone))
    return (
        f"วัน{_THAI_DAYS[now.weekday()]}ที่ {now.day} {_THAI_MONTHS[now.month - 1]} "
        f"ค.ศ. {now.year} (พ.ศ. {now.year + 543}) เวลา {now:%H:%M} น. ({timezone})"
    )
FENCE = "```"


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """แบ่งข้อความเป็นหลายก้อน แต่ละก้อนยาวไม่เกิน limit

    พยายามตัดที่ขึ้นบรรทัดใหม่ก่อน แล้วค่อยเป็นช่องว่าง ถ้าไม่มีจึงตัดตรง ๆ
    (ภาษาไทยไม่เว้นวรรคระหว่างคำ จึงอาจต้องตัดกลางประโยค)
    ถ้าตัดกลาง code block (```) จะปิด block แล้วเปิดใหม่ในก้อนถัดไปให้อัตโนมัติ
    """
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    reopen = ""  # บรรทัดเปิด code block ที่ต้องต่อในก้อนถัดไป เช่น "```python"
    # เผื่อที่สำหรับ "\n```" ที่ต้องเติมปิดท้ายก้อน
    budget = limit - len(FENCE) - 1

    while text:
        text = reopen + text if reopen else text
        if len(text) <= limit:
            chunks.append(text)
            break

        window = text[: budget]
        cut = window.rfind("\n")
        if cut < budget // 2:
            cut = window.rfind(" ")
        if cut < budget // 2:
            cut = budget

        chunk, text = text[:cut].rstrip(), text[cut:].lstrip("\n ")
        reopen = ""
        open_fence = _unclosed_fence(chunk)
        if open_fence is not None:
            chunk += "\n" + FENCE
            reopen = open_fence + "\n"
        if chunk:
            chunks.append(chunk)

    return chunks


def _unclosed_fence(chunk: str) -> str | None:
    """ถ้าก้อนข้อความมี code block ที่ยังไม่ปิด คืนบรรทัดเปิด block นั้น (เช่น ```py)"""
    opener: str | None = None
    for line in chunk.split("\n"):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            opener = None if opener is not None else stripped
    return opener
