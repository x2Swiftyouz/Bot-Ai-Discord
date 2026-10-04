"""ดึงรูปจากไฟล์แนบของ Discord เพื่อส่งให้ AI อ่าน"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import discord

from .providers import ImageData

log = logging.getLogger(__name__)

# ชนิดรูปที่ทั้ง Gemini และโมเดล vision แบบ OpenAI รองรับแน่นอน
SUPPORTED_TYPES = {"image/png", "image/jpeg", "image/webp"}
_EXT_TO_TYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _mime_of(attachment: discord.Attachment) -> str | None:
    mime = (attachment.content_type or "").split(";")[0].strip().lower()
    if mime in SUPPORTED_TYPES:
        return mime
    name = attachment.filename.lower()
    for ext, ext_mime in _EXT_TO_TYPE.items():
        if name.endswith(ext):
            return ext_mime
    return None


def is_image(attachment: discord.Attachment) -> bool:
    mime = (attachment.content_type or "").lower()
    return mime.startswith("image/") or _mime_of(attachment) is not None


async def read_images(
    attachments: Iterable[discord.Attachment], max_count: int, max_bytes: int
) -> tuple[list[ImageData], list[str]]:
    """คืน (รูปที่อ่านได้, รายการเหตุผลของรูปที่ถูกข้าม)"""
    images: list[ImageData] = []
    skipped: list[str] = []
    for att in attachments:
        if not is_image(att):
            continue
        mime = _mime_of(att)
        if mime is None:
            skipped.append(f"`{att.filename}` (รองรับแค่ png / jpg / webp)")
        elif att.size > max_bytes:
            skipped.append(f"`{att.filename}` (ใหญ่เกิน {max_bytes // (1024 * 1024)} MB)")
        elif len(images) >= max_count:
            skipped.append(f"`{att.filename}` (เกิน {max_count} รูปต่อข้อความ)")
        else:
            try:
                images.append(ImageData(mime, await att.read()))
            except discord.HTTPException:
                log.warning("Failed to download attachment %s", att.filename)
                skipped.append(f"`{att.filename}` (ดาวน์โหลดไม่สำเร็จ)")
    return images, skipped
