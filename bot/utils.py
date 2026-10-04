"""ฟังก์ชันช่วยเหลือ: ตัดข้อความยาวให้ไม่เกินข้อจำกัด 2000 ตัวอักษรของ Discord"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

DISCORD_LIMIT = 2000

# รูปแบบ API key ของ Google (AIza..., AQ....), Groq (gsk_...), OpenRouter/OpenAI (sk-...)
# และ token บอท Discord (3 ท่อนคั่นด้วยจุด)
_SECRET_RE = re.compile(
    r"AIza[0-9A-Za-z_\-]{20,}"
    r"|AQ\.[0-9A-Za-z_\-]{20,}"
    r"|gsk_[0-9A-Za-z]{20,}"
    r"|sk-[0-9A-Za-z_\-]{20,}"
    r"|tvly-[0-9A-Za-z_\-]{16,}"
    r"|[MNO][0-9A-Za-z_\-]{20,}\.[0-9A-Za-z_\-]{4,}\.[0-9A-Za-z_\-]{20,}"
)


def looks_like_secret(text: str) -> bool:
    return bool(_SECRET_RE.fullmatch(text.strip()))


def redact(text: str) -> str:
    """ซ่อน API key / token ในข้อความ (ใช้กับ log)"""
    return _SECRET_RE.sub("***", text)

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


# ---------- ตาราง Markdown → รายการ (Discord แสดงตารางไม่ได้) ----------

# <br> ของ HTML ที่ AI ชอบใส่ในตาราง — Discord แสดงเป็นตัวอักษรตรง ๆ
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") or line.count("|") >= 2


def tables_to_lists(text: str) -> str:
    """แปลงตาราง Markdown เป็นรายการ bullet (เว้นตารางที่อยู่ใน code block)

    | ตัวเลือก | ค่า | คำอธิบาย |     →    - **Gyro X** — ค่า: 1.7 · คำอธิบาย: หมุนซ้าย-ขวา
    """
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
        if (
            not in_code
            and i + 1 < len(lines)
            and "|" in line
            and _TABLE_SEPARATOR.match(lines[i + 1])
        ):
            headers = _cells(line)
            i += 2
            while i < len(lines) and lines[i].strip() and _is_table_row(lines[i]):
                # <br> ในช่องตาราง → ขึ้นบรรทัดใหม่แบบย่อหน้าต่อจาก bullet
                row = [_HTML_BREAK.sub("\n  ", c).strip() for c in _cells(lines[i])]
                first = row[0] if row else ""
                if first and not first.startswith("**"):
                    first = f"**{first}**"
                rest = [
                    f"{h}: {v}" if h else v
                    for h, v in zip(headers[1:], row[1:], strict=False)
                    if v
                ]
                out.append(f"- {first} — {' · '.join(rest)}" if rest else f"- {first}")
                i += 1
            continue
        out.append(line if in_code else _HTML_BREAK.sub("\n", line))
        i += 1
    return "\n".join(out)


# ลิงก์เปล่า ๆ (ไม่อยู่ใน <...> หรือ [ข้อความ](ลิงก์)) — Discord จะแสดงพรีวิวการ์ดใหญ่
_BARE_URL = re.compile(r"(?<![<(\w])(https?://[^\s<>()\[\]]+[^\s<>()\[\].,!?;:'\"])")
_MASKED_URL = re.compile(r"\]\((https?://[^\s<>()]+)\)")


def suppress_link_previews(text: str) -> str:
    """ครอบลิงก์เปล่าด้วย <...> เพื่อไม่ให้ Discord แสดงพรีวิวใหญ่ ๆ (ยังกดเปิดได้เหมือนเดิม)"""
    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
        if not in_code:
            line = _MASKED_URL.sub(r"](<\1>)", _BARE_URL.sub(r"<\1>", line))
        out.append(line)
    return "\n".join(out)
