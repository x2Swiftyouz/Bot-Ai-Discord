from datetime import datetime
from zoneinfo import ZoneInfo

from bot.utils import (
    looks_like_secret,
    now_text,
    redact,
    split_message,
    suppress_link_previews,
    tables_to_lists,
)


def test_split_message_respects_limit_and_code_fences():
    text = ("สวัสดีครับ" * 300) + "\n```python\n" + ("print('x')\n" * 300) + "```\nจบ"
    chunks = split_message(text)
    assert all(len(c) <= 2000 for c in chunks)
    # ตัดกลาง code block ต้องปิด/เปิด ``` ให้ครบทุกก้อน
    assert all(c.count("```") % 2 == 0 for c in chunks)
    assert split_message("   ") == []


def test_tables_to_lists():
    text = (
        "| ตัวเลือก | ค่า | คำอธิบาย |\n|---|:-:|---|\n"
        "| Gyro X | 1.8 | ซ้าย-ขวา <br>ทดสอบก่อน |\n| **Deadzone** | 2% | |"
    )
    out = tables_to_lists(text)
    assert "|" not in out
    assert "- **Gyro X** — ค่า: 1.8 · คำอธิบาย: ซ้าย-ขวา\n  ทดสอบก่อน" in out
    assert "- **Deadzone** — ค่า: 2%" in out


def test_tables_inside_code_blocks_are_kept():
    text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    assert tables_to_lists(text) == text


def test_suppress_link_previews():
    assert suppress_link_previews("ดู https://youtu.be/x ได้เลย") == "ดู <https://youtu.be/x> ได้เลย"
    assert suppress_link_previews("[ชื่อ](https://a.com)") == "[ชื่อ](<https://a.com>)"
    assert suppress_link_previews("<https://a.com>") == "<https://a.com>"
    assert suppress_link_previews("```\nhttps://a.com\n```") == "```\nhttps://a.com\n```"


def test_secret_detection_and_redaction():
    assert looks_like_secret("gsk_" + "a" * 40)
    assert looks_like_secret("AQ.Ab8" + "x" * 40)
    assert not looks_like_secret("openai/gpt-oss-120b")
    assert redact("key=gsk_" + "b" * 40) == "key=***"


def test_now_text_in_thai():
    when = datetime(2026, 10, 4, 8, 5, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert now_text("Asia/Bangkok", when) == (
        "วันอาทิตย์ที่ 4 ตุลาคม ค.ศ. 2026 (พ.ศ. 2569) เวลา 08:05 น. (Asia/Bangkok)"
    )
