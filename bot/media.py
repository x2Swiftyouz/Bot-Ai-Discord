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


# ---------- ไฟล์เอกสาร: PDF / ไฟล์ข้อความ / โค้ด ----------

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".log", ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".java", ".kt", ".c", ".h", ".cpp",
    ".cs", ".go", ".rs", ".lua", ".luau", ".sql", ".sh", ".bat", ".ps1", ".php", ".rb", ".swift",
    ".dart", ".vue", ".env.example",
}


def _ext(attachment: discord.Attachment) -> str:
    name = attachment.filename.lower()
    return name[name.rfind("."):] if "." in name else ""


def is_pdf(attachment: discord.Attachment) -> bool:
    mime = (attachment.content_type or "").lower()
    return mime.startswith("application/pdf") or _ext(attachment) == ".pdf"


def is_text_file(attachment: discord.Attachment) -> bool:
    mime = (attachment.content_type or "").split(";")[0].strip().lower()
    return _ext(attachment) in TEXT_EXTENSIONS or mime.startswith("text/") or mime in (
        "application/json", "application/xml", "application/javascript",
    )


def is_document(attachment: discord.Attachment) -> bool:
    return is_pdf(attachment) or is_text_file(attachment)


def _pdf_text(data: bytes, max_chars: int) -> str:
    """ดึงตัวหนังสือจาก PDF (PDF ที่เป็นภาพสแกนจะได้ข้อความว่าง)"""
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    total = 0
    for page_no, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(f"--- หน้า {page_no} ---\n{text}")
            total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars]


async def read_documents(
    attachments: Iterable[discord.Attachment], max_bytes: int, max_chars: int
) -> tuple[str, list[ImageData], list[str], list[str]]:
    """อ่านไฟล์เอกสารที่แนบมา

    คืน (ข้อความของไฟล์ทั้งหมดสำหรับแนบไปกับคำถาม, PDF ที่ดึงข้อความไม่ได้ (ส่งให้ AI อ่านเอง),
         ชื่อไฟล์ที่อ่านได้, เหตุผลของไฟล์ที่ถูกข้าม)
    """
    import asyncio

    blocks: list[str] = []
    inline_pdfs: list[ImageData] = []
    names: list[str] = []
    skipped: list[str] = []
    remaining = max_chars
    for att in attachments:
        if not is_document(att):
            continue
        if att.size > max_bytes:
            skipped.append(f"`{att.filename}` (ใหญ่เกิน {max_bytes // (1024 * 1024)} MB)")
            continue
        if remaining <= 0:
            skipped.append(f"`{att.filename}` (ไฟล์รวมกันยาวเกินไป)")
            continue
        try:
            data = await att.read()
        except discord.HTTPException:
            skipped.append(f"`{att.filename}` (ดาวน์โหลดไม่สำเร็จ)")
            continue
        if is_pdf(att):
            try:
                text = await asyncio.to_thread(_pdf_text, data, remaining)
            except Exception as e:  # PDF เสีย / เข้ารหัส
                log.warning("อ่าน PDF %s ไม่ได้: %r", att.filename, e)
                skipped.append(f"`{att.filename}` (เปิด PDF ไม่ได้ อาจเสียหรือมีรหัสผ่าน)")
                continue
            if not text.strip():
                # PDF ที่เป็นภาพสแกน: ส่งไฟล์ให้ AI อ่านเอง (Gemini อ่าน PDF ได้)
                inline_pdfs.append(ImageData("application/pdf", data))
                names.append(att.filename)
                continue
        else:
            text = data.decode("utf-8", errors="replace")[:remaining]
        remaining -= len(text)
        names.append(att.filename)
        lang = _ext(att).lstrip(".") if is_text_file(att) and not is_pdf(att) else ""
        blocks.append(f"[ไฟล์แนบ: {att.filename}]\n```{lang}\n{text}\n```")
    return "\n\n".join(blocks), inline_pdfs, names, skipped
