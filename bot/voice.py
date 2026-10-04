"""ถอดเสียงจากข้อความเสียง / ไฟล์เสียงที่แนบมา ด้วย Groq Whisper (ฟรี ใช้ GROQ_API_KEY เดิม)

ส่งข้อความเสียงในห้อง AI (หรือ mention บอทพร้อมไฟล์เสียง) → ถอดเป็นข้อความ → ถาม AI ต่อตามปกติ
"""

from __future__ import annotations

import logging

import aiohttp
import discord

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".flac", ".mp4")


def is_audio(attachment: discord.Attachment) -> bool:
    mime = (attachment.content_type or "").lower()
    return mime.startswith("audio/") or attachment.filename.lower().endswith(AUDIO_EXTENSIONS)


class TranscriptionError(Exception):
    pass


class Transcriber:
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, api_keys: tuple[str, ...], model: str, timeout: float = 60) -> None:
        self.api_keys = api_keys
        self.model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def transcribe(self, data: bytes, filename: str, content_type: str | None) -> str:
        """คืนข้อความที่ถอดได้ (ลอง key ถัดไปถ้า key แรกเกินโควต้า/ใช้ไม่ได้)"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        last_error = "no API key"
        for key in self.api_keys:
            form = aiohttp.FormData()
            form.add_field("file", data, filename=filename, content_type=content_type or "audio/ogg")
            form.add_field("model", self.model)
            form.add_field("response_format", "json")
            try:
                async with self._session.post(
                    self.URL, data=form, headers={"Authorization": f"Bearer {key}"}
                ) as resp:
                    if resp.status in (401, 403, 429):
                        last_error = f"HTTP {resp.status}"
                        continue
                    if resp.status >= 400:
                        raise TranscriptionError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                    payload = await resp.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError) as e:
                raise TranscriptionError(f"connection error: {e!r}") from e
            text = (payload.get("text") or "").strip()
            log.info("ถอดเสียง %s (%d ไบต์) → %d ตัวอักษร", filename, len(data), len(text))
            return text
        raise TranscriptionError(last_error)
