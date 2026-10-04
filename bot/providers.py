"""ตัวเชื่อมต่อ AI API (async ทั้งหมดด้วย aiohttp)

- Gemini  : Google Generative Language REST API
- Groq    : OpenAI-compatible API
- OpenRouter : OpenAI-compatible API (ใช้โมเดลที่ลงท้าย :free)

เพิ่มผู้ให้บริการใหม่ได้โดยสืบทอด AIProvider แล้วเพิ่มใน create_provider()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace

import aiohttp

from .config import Config
from .memory import ChatMessage
from .utils import now_text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageData:
    """รูปที่แนบมากับคำถาม (ส่งให้ AI เฉพาะคำถามปัจจุบัน ไม่เก็บลงความจำ)"""

    mime_type: str
    data: bytes

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


# บุคลิกของห้องที่กำลังตอบ (/persona) — ตั้งโดยบอทก่อนเรียก AI แทนที่ SYSTEM_PROMPT
CURRENT_PERSONA: ContextVar[str | None] = ContextVar("CURRENT_PERSONA", default=None)
# ข้อมูลที่ผู้ถามขอให้จำไว้ (/remember) — ต่อท้าย system prompt ของคำถามนั้น
CURRENT_USER_NOTES: ContextVar[str | None] = ContextVar("CURRENT_USER_NOTES", default=None)

# บอก AI ว่าบอทจัดการข้อความยาวให้เอง — ไม่งั้น AI มักปฏิเสธงานยาวเพราะคิดว่าติดข้อจำกัด 2,000 ตัวอักษรของ Discord
PLATFORM_NOTE = (
    "หมายเหตุระบบ: คุณกำลังตอบในแชต Discord ผ่านบอทที่แบ่งข้อความยาวเป็นหลายข้อความ "
    "หรือแนบเป็นไฟล์ .txt ให้อัตโนมัติ จึงไม่ต้องกังวลเรื่องข้อจำกัดความยาวของ Discord "
    "ถ้าผู้ใช้ขอเนื้อหายาว (เช่น บทความหลายพันคำ) ให้เขียนเต็มความยาวที่ขอได้เลยในคำตอบเดียว "
    "ไม่ต้องเสนอแบ่งส่วนหรือส่งไฟล์ Word/Google Docs เพราะคุณทำสิ่งนั้นไม่ได้"
)

# รับ "ข้อความทั้งหมดที่ได้มาถึงตอนนี้" ระหว่าง streaming (ใช้แสดงคำตอบค่อย ๆ พิมพ์)
OnDelta = Callable[[str], None]


@dataclass(frozen=True)
class AIResult:
    text: str
    model: str  # โมเดลที่ตอบจริง (อาจเป็นโมเดลสำรอง)
    sources: tuple[tuple[str, str], ...] = ()  # (ชื่อเว็บ, ลิงก์) จากการค้นเว็บ
    searched: bool = False
    backup: bool = False  # ตอบโดยผู้ให้บริการสำรอง


class AIError(Exception):
    """ข้อผิดพลาดทั่วไปจาก AI API — ข้อความใน user_message จะถูกส่งให้ผู้ใช้เห็น"""

    user_message = "⚠️ ระบบ AI มีปัญหาชั่วคราว ลองใหม่อีกครั้งในภายหลังนะ"


class RateLimitError(AIError):
    user_message = "⏳ ตอนนี้มีคนใช้เยอะจนเกินโควต้าฟรีของ AI (HTTP 429) รอสักครู่แล้วลองใหม่นะ"

    def __init__(self, retry_after: float | None = None, body: str = "") -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after
        self.body = body
        if retry_after:
            self.user_message += f" (ประมาณ {int(retry_after) + 1} วินาที)"


class AITimeoutError(AIError):
    user_message = "⌛ AI ใช้เวลาตอบนานเกินไป ลองถามใหม่อีกครั้งนะ"


class AIBlockedError(AIError):
    user_message = "🚫 AI ปฏิเสธที่จะตอบคำถามนี้ (ถูกตัวกรองความปลอดภัยบล็อก) ลองเปลี่ยนคำถามดูนะ"


class BadRequestError(AIError):
    """HTTP 400 — คำขอไม่ถูกต้อง (เช่น โมเดลไม่รองรับฟีเจอร์ที่ขอ)"""


class ServiceUnavailableError(AIError):
    user_message = "🔥 เซิร์ฟเวอร์ AI มีคนใช้งานหนาแน่นชั่วคราว รอสักครู่แล้วลองใหม่นะ"


class ModelNotFoundError(AIError):
    user_message = "🧩 ไม่พบโมเดล AI ที่ตั้งค่าไว้ (อาจถูกถอดแล้ว) แจ้งผู้ดูแลบอทให้เปลี่ยนชื่อโมเดลในไฟล์ .env"


class AuthError(AIError):
    user_message = "🔑 API key ไม่ถูกต้องหรือหมดอายุ แจ้งผู้ดูแลบอทให้ตรวจสอบไฟล์ .env"


def _parse_retry_after(headers) -> float | None:
    raw = headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class AIProvider(ABC):
    name: str = "base"

    def __init__(self, config: Config) -> None:
        self.api_keys = config.api_keys
        self.model = config.model
        # ลองโมเดลหลักก่อน ถ้าล่ม/เกินโควต้า/ถูกถอด ค่อยไล่ลองโมเดลสำรองตามลำดับ
        self.models = list(dict.fromkeys((config.model, *config.fallback_models)))
        # โมเดลที่ใช้เมื่อคำถามมีรูป (ว่าง = ใช้โมเดลปกติ)
        self.vision_model = config.vision_model
        self.max_retries = config.max_retries
        self.system_prompt = config.system_prompt
        self.timezone = config.timezone
        self.temperature = config.temperature
        # มี Tavily แล้วไม่ต้องใช้ค้นเว็บของ Gemini (ซึ่ง free tier มักไม่มีโควต้า)
        self.web_search = config.web_search and not config.tavily_api_key
        self.streaming = config.streaming
        self._timeout = aiohttp.ClientTimeout(total=config.ai_timeout)
        self._session: aiohttp.ClientSession | None = None
        # (ลำดับ key, โมเดล) ที่เกินโควต้า -> เวลาที่จะลองใช้ได้อีก
        self._key_paused_until: dict[tuple[int, str], float] = {}

    KEY_PAUSE_SECONDS = 60

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post_json(self, url: str, payload: dict, headers: dict, model: str) -> dict:
        """ส่ง POST แล้วแปลง error ของ HTTP ให้เป็น exception ที่บอทเข้าใจ"""
        session = await self._get_session()
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                await self._raise_for_status(resp, model)
                try:
                    return await resp.json(content_type=None)
                except ValueError as e:
                    raise AIError("invalid JSON response") from e
        except TimeoutError as e:  # aiohttp ใช้ asyncio.TimeoutError (= TimeoutError ใน 3.11+)
            raise AITimeoutError("timeout") from e
        except aiohttp.ClientError as e:
            log.error("%s connection error: %r", self.name, e)
            raise AIError("connection error") from e

    async def _post_stream(
        self, url: str, payload: dict, headers: dict, model: str
    ) -> AsyncIterator[dict]:
        """ส่ง POST แบบ streaming (Server-Sent Events) แล้วคืน JSON ทีละก้อนที่ได้รับ"""
        session = await self._get_session()
        # streaming ใช้เวลารวมนานได้ จึงจำกัดแค่ "เงียบนานเกิน" แทนเวลารวม
        timeout = aiohttp.ClientTimeout(total=None, sock_read=self._timeout.total)
        try:
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                await self._raise_for_status(resp, model)
                buffer = b""
                async for chunk in resp.content.iter_any():
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            return
                        try:
                            yield json.loads(data)
                        except ValueError:
                            log.debug("%s: skip bad SSE line %r", self.name, data[:200])
        except TimeoutError as e:
            raise AITimeoutError("timeout") from e
        except aiohttp.ClientError as e:
            log.error("%s connection error: %r", self.name, e)
            raise AIError("connection error") from e

    async def _get_json(self, url: str, headers: dict) -> dict:
        session = await self._get_session()
        try:
            async with session.get(url, headers=headers) as resp:
                await self._raise_for_status(resp, "-")
                return await resp.json(content_type=None)
        except TimeoutError as e:
            raise AITimeoutError("timeout") from e
        except (aiohttp.ClientError, ValueError) as e:
            raise AIError(f"list models failed: {e!r}") from e

    async def available_models(self) -> set[str] | None:
        """รายชื่อโมเดลที่ใช้ได้กับ key นี้ (None = เช็กไม่ได้)"""
        return None

    @staticmethod
    def suggest(available: set[str]) -> list[str]:
        """เลือกชื่อโมเดลตัวอย่างไว้แสดงใน log"""
        return sorted(available)[:10]

    async def check_models(self) -> list[str]:
        """ตอนเริ่มบอท: เช็กว่าชื่อโมเดลใน .env ยังมีอยู่จริง (โมเดลฟรีถูกถอดบ่อย)

        เตือนใน log และคืนรายการปัญหาที่เจอ (ไว้ส่งเข้าห้อง log ของแอดมิน)
        """
        try:
            available = await self.available_models()
        except AIError as e:
            log.warning("เช็กรายชื่อโมเดลของ %s ไม่ได้ (%r) ข้ามการตรวจ", self.name, e)
            return []
        if not available:
            return []
        configured = list(dict.fromkeys((*self.models, *([self.vision_model] if self.vision_model else []))))
        missing = [m for m in configured if m not in available]
        if not missing:
            log.info("ตรวจโมเดล %s: %s ใช้ได้ ✅", self.name, ", ".join(configured))
            return []
        prefix = self.name.upper()
        problems = []
        for model in missing:
            message = (
                f"⚠️ {self.name} ไม่มีโมเดล {model!r} แล้ว (ถูกถอดหรือพิมพ์ผิด) — แก้ "
                f"{prefix}_MODEL / {prefix}_FALLBACK_MODELS / {prefix}_VISION_MODEL ใน .env | "
                f"ตัวอย่างโมเดลที่ใช้ได้ตอนนี้: {', '.join(self.suggest(available))}"
            )
            log.error("%s", message)
            problems.append(message)
        return problems

    async def _raise_for_status(self, resp: aiohttp.ClientResponse, model: str) -> None:
        """แปลง HTTP error เป็น exception ที่บอทเข้าใจ (ไม่ทำอะไรถ้าสำเร็จ)"""
        if resp.status == 429:
            # ข้อความใน body บอกว่าโควต้าตัวไหนหมด (ต่อนาที / ต่อวัน / ค้นเว็บ)
            body = await resp.text()
            log.warning("%s HTTP 429 (model %s): %s", self.name, model, body[:800])
            raise RateLimitError(_parse_retry_after(resp.headers), body)
        if resp.status in (401, 403):
            log.error("%s auth error %s: %s", self.name, resp.status, await resp.text())
            raise AuthError(f"HTTP {resp.status}")
        if resp.status == 404:
            log.error(
                "%s HTTP 404 — model %r not found, update *_MODEL in .env: %s",
                self.name, model, (await resp.text())[:500],
            )
            raise ModelNotFoundError("HTTP 404")
        if resp.status in (500, 502, 503, 504):
            log.warning(
                "%s HTTP %s (model %s): %s",
                self.name, resp.status, model, (await resp.text())[:300],
            )
            raise ServiceUnavailableError(f"HTTP {resp.status}")
        if resp.status == 400:
            log.error("%s HTTP 400 (model %s): %s", self.name, model, (await resp.text())[:500])
            raise BadRequestError("HTTP 400")
        if resp.status >= 400:
            body = await resp.text()
            log.error("%s HTTP %s: %s", self.name, resp.status, body[:500])
            raise AIError(f"HTTP {resp.status}")

    def build_system_prompt(self) -> str:
        """system prompt + วันเวลาปัจจุบัน (AI ไม่รู้วันที่เองจึงคำนวณระยะเวลาผิดถ้าไม่บอก)"""
        now = f"ข้อมูลอ้างอิง: ตอนนี้คือ{now_text(self.timezone)} ใช้ข้อมูลนี้เมื่อต้องคำนวณวันเวลา"
        base = CURRENT_PERSONA.get() or self.system_prompt
        parts = [p for p in (base, now, PLATFORM_NOTE, CURRENT_USER_NOTES.get()) if p]
        return "\n\n".join(parts)

    async def generate(
        self,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData] = (),
        on_delta: OnDelta | None = None,
        retry: bool = True,
    ) -> AIResult:
        """รับประวัติบทสนทนา + คำถามใหม่ (+ รูปถ้ามี) คืนข้อความคำตอบ

        on_delta: ถ้าส่งมา (และเปิด STREAMING) จะถูกเรียกด้วยข้อความที่ได้มาเรื่อย ๆ ระหว่างรอ
        retry=False: ไม่ลองซ้ำเมื่อเซิร์ฟเวอร์ล่ม (ใช้เมื่อมีตัวสำรองที่ตอบแทนได้ทันที)

        - เซิร์ฟเวอร์ล่มชั่วคราว (5xx): ลองซ้ำกับโมเดลเดิม โดยรอนานขึ้นเรื่อย ๆ (1, 2, 4 ... วินาที)
        - ล่มต่อเนื่อง / เกินโควต้า (429) / ไม่พบโมเดล (404): ข้ามไปโมเดลสำรองถัดไป
        - error อื่น (เช่น key ผิด, ถูกบล็อก): หยุดทันที เพราะลองใหม่ก็ไม่ช่วย
        ถ้าทุกโมเดลล้มเหลว จะ raise error ล่าสุดเพื่อให้บอทแจ้งผู้ใช้
        """
        last_error: AIError | None = None
        models = self.models
        if images and self.vision_model:
            models = list(dict.fromkeys((self.vision_model, *self.models)))
        for model in models:
            now = time.monotonic()
            keys = [
                (no, key) for no, key in enumerate(self.api_keys)
                if self._key_paused_until.get((no, model), 0) <= now
            ]
            if not keys:
                last_error = last_error or RateLimitError()
            for no, key in keys:
                try:
                    return await self._generate_with_retry(
                        model, key, history, prompt, images, on_delta, retry
                    )
                except RateLimitError as e:
                    # key นี้เกินโควต้า → พักไว้ แล้วลอง key ถัดไป (key จากคนละโปรเจกต์ได้โควต้าแยกกัน)
                    last_error = e
                    pause = max(e.retry_after or 0, self.KEY_PAUSE_SECONDS)
                    self._key_paused_until[(no, model)] = time.monotonic() + pause
                    if len(self.api_keys) > 1:
                        log.warning("%s key #%d เกินโควต้าของ %s พักไว้ %.0f วินาที",
                                    self.name, no + 1, model, pause)
                except AuthError as e:
                    # key ผิด/ถูกลบ → ถ้ามี key อื่นก็ลองต่อ
                    last_error = e
                    log.error("%s key #%d ใช้ไม่ได้ (key ผิดหรือถูกลบ)", self.name, no + 1)
                except (ServiceUnavailableError, ModelNotFoundError) as e:
                    # ปัญหาที่ตัวโมเดล ไม่เกี่ยวกับ key → ข้ามไปโมเดลถัดไป
                    last_error = e
                    break
            if model != models[-1]:
                log.warning("%s model %s failed (%r), trying fallback", self.name, model, last_error)
        assert last_error is not None
        raise last_error

    async def _generate_with_retry(
        self,
        model: str,
        key: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
        on_delta: OnDelta | None,
        retry: bool,
    ) -> AIResult:
        """เรียก API ด้วยโมเดล + key ที่กำหนด ถ้าเซิร์ฟเวอร์ล่ม (5xx) ลองซ้ำโดยรอ 1, 2, 4 ... วินาที"""
        max_retries = self.max_retries if retry else 0
        stream = on_delta if self.streaming else None
        for attempt in range(max_retries + 1):
            try:
                return await self._generate(model, key, history, prompt, images, stream)
            except ServiceUnavailableError:
                if attempt >= max_retries:
                    raise
                delay = 2**attempt
                log.info("%s %s busy, retrying in %ss", self.name, model, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @abstractmethod
    async def _generate(
        self,
        model: str,
        api_key: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
        on_delta: OnDelta | None,
    ) -> AIResult:
        """เรียก API หนึ่งครั้งด้วยโมเดลที่กำหนด คืนข้อความคำตอบ (stream ถ้ามี on_delta)"""


class GeminiProvider(AIProvider):
    name = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    # โดน 429 ตอนค้นเว็บ → พักการค้นเว็บของโมเดลนั้นกี่วินาที
    SEARCH_PAUSE_SECONDS = 30 * 60
    # ถ้าตอบแบบไม่ค้นเว็บได้ แปลว่าโควต้า "ค้นเว็บ" หมดโดยเฉพาะ (หรือ free tier ไม่มีให้) → พักนานขึ้น
    SEARCH_QUOTA_PAUSE_SECONDS = 6 * 60 * 60
    # ถ้าไม่บอก Gemini มักไม่ค้นเอง และตอบว่า "เข้าถึงข้อมูลเรียลไทม์ไม่ได้"
    SEARCH_HINT = (
        "คุณมีเครื่องมือ Google Search ใช้ค้นข้อมูลล่าสุดได้ "
        "เมื่อถูกถามเรื่องข่าว เหตุการณ์ปัจจุบัน ราคา ผลกีฬา สภาพอากาศ เวอร์ชันล่าสุด "
        "หรือข้อมูลที่อาจเปลี่ยนแปลงหลังจากข้อมูลที่คุณเรียนรู้มา ให้ค้นก่อนตอบเสมอ "
        "ห้ามตอบว่าเข้าถึงข้อมูลเรียลไทม์ไม่ได้"
    )

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        # โมเดลที่ใช้ค้นเว็บไม่ได้ (ตอบ 400) จะไม่ขอค้นเว็บอีกจนกว่าจะรีสตาร์ท
        self._no_search_models: set[str] = set()
        # โมเดลที่โควต้าค้นเว็บหมด: model -> เวลาที่จะลองค้นเว็บได้อีก
        self._search_paused_until: dict[str, float] = {}

    async def available_models(self) -> set[str] | None:
        data = await self._get_json(
            f"{self.BASE_URL}?pageSize=1000", {"x-goog-api-key": self.api_keys[0]}
        )
        return {
            m["name"].removeprefix("models/")
            for m in data.get("models") or []
            if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])
        }

    @staticmethod
    def suggest(available: set[str]) -> list[str]:
        # รุ่นใหม่ก่อน เฉพาะตระกูล flash / pro
        names = [m for m in available if "flash" in m or "pro" in m]
        return sorted(names, reverse=True)[:10]

    def _search_enabled(self, model: str) -> bool:
        if not self.web_search or model in self._no_search_models:
            return False
        return time.monotonic() >= self._search_paused_until.get(model, 0)

    async def _generate(
        self,
        model: str,
        api_key: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
        on_delta: OnDelta | None,
    ) -> AIResult:
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in history
        ]
        parts: list[dict] = [
            {"inline_data": {"mime_type": img.mime_type, "data": img.b64()}} for img in images
        ]
        parts.append({"text": prompt})
        contents.append({"role": "user", "parts": parts})

        system_prompt = self.build_system_prompt()
        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self.temperature},
            "systemInstruction": {"parts": [{"text": system_prompt}]},
        }
        search = self._search_enabled(model)
        if search:
            # Gemini ตัดสินใจเองว่าจะค้น Google ไหม (ค้นเฉพาะคำถามที่ต้องใช้ข้อมูลล่าสุด)
            payload["tools"] = [{"google_search": {}}]
            payload["systemInstruction"] = {
                "parts": [{"text": f"{system_prompt}\n\n{self.SEARCH_HINT}"}]
            }

        def without_search() -> None:
            payload.pop("tools")
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        # ส่ง key ทาง header แทน query string เพื่อไม่ให้ key หลุดไปใน log ของ URL
        headers = {"x-goog-api-key": api_key}

        async def request() -> dict:
            if on_delta is None:
                url = f"{self.BASE_URL}/{model}:generateContent"
                return await self._post_json(url, payload, headers, model)
            url = f"{self.BASE_URL}/{model}:streamGenerateContent?alt=sse"
            return await self._collect_stream(url, payload, headers, model, on_delta)

        try:
            data = await request()
        except BadRequestError:
            if not search:
                raise
            log.warning("Gemini %s ใช้ค้นเว็บไม่ได้ จะตอบแบบไม่ค้นเว็บแทน", model)
            self._no_search_models.add(model)
            without_search()
            data = await request()
        except RateLimitError:
            if not search:
                raise
            # โควต้าค้นเว็บมักหมดก่อนโควต้าปกติ ลองตอบแบบไม่ค้นเว็บอีกครั้ง
            # ถ้ายังโดน 429 แปลว่าโควต้าปกติหมดด้วย ให้ error ส่งต่อไปตามปกติ
            minutes = self.SEARCH_PAUSE_SECONDS // 60
            log.warning("Gemini %s โดน 429 ตอนค้นเว็บ พักการค้นเว็บ %s นาที", model, minutes)
            self._search_paused_until[model] = time.monotonic() + self.SEARCH_PAUSE_SECONDS
            without_search()
            data = await request()
            hours = self.SEARCH_QUOTA_PAUSE_SECONDS // 3600
            log.warning(
                "Gemini %s ตอบได้เมื่อไม่ค้นเว็บ → โควต้าค้นเว็บหมด (หรือ free tier ไม่มีให้) "
                "พักการค้นเว็บ %s ชั่วโมง — ถ้าเป็นแบบนี้ทุกวัน ให้ตั้ง WEB_SEARCH=false",
                model, hours,
            )
            self._search_paused_until[model] = time.monotonic() + self.SEARCH_QUOTA_PAUSE_SECONDS

        if block := data.get("promptFeedback", {}).get("blockReason"):
            log.warning("Gemini blocked prompt: %s", block)
            raise AIBlockedError(block)

        candidates = data.get("candidates") or []
        if not candidates:
            raise AIError("no candidates")
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
                raise AIBlockedError(candidate["finishReason"])
            raise AIError(f"empty response (finishReason={candidate.get('finishReason')})")

        grounding = candidate.get("groundingMetadata") or {}
        if search and grounding.get("webSearchQueries"):
            log.info("Gemini ค้นเว็บ: %s", grounding["webSearchQueries"])
        sources: list[tuple[str, str]] = []
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            if (uri := web.get("uri")) and all(uri != u for _, u in sources):
                sources.append((web.get("title") or "ลิงก์", uri))
        return AIResult(
            text, model, tuple(sources), searched=bool(grounding.get("webSearchQueries"))
        )

    async def _collect_stream(
        self, url: str, payload: dict, headers: dict, model: str, on_delta: OnDelta
    ) -> dict:
        """รับคำตอบแบบ stream แล้วรวมเป็นรูปแบบเดียวกับ generateContent ปกติ"""
        text = ""
        candidate: dict = {}
        prompt_feedback: dict = {}
        async for chunk in self._post_stream(url, payload, headers, model):
            prompt_feedback = chunk.get("promptFeedback") or prompt_feedback
            for cand in (chunk.get("candidates") or [])[:1]:
                for part in (cand.get("content") or {}).get("parts") or []:
                    if part.get("text") and not part.get("thought"):
                        text += part["text"]
                        on_delta(text)
                if cand.get("finishReason"):
                    candidate["finishReason"] = cand["finishReason"]
                if cand.get("groundingMetadata"):
                    candidate["groundingMetadata"] = cand["groundingMetadata"]
        candidate["content"] = {"parts": [{"text": text}]}
        return {"candidates": [candidate], "promptFeedback": prompt_feedback}


class OpenAICompatibleProvider(AIProvider):
    """ใช้ได้กับทุก API ที่รองรับรูปแบบ OpenAI Chat Completions (Groq, OpenRouter ฯลฯ)"""

    url: str = ""

    def extra_headers(self) -> dict:
        return {}

    async def available_models(self) -> set[str] | None:
        url = self.url.rsplit("/chat/completions", 1)[0] + "/models"
        headers = {"Authorization": f"Bearer {self.api_keys[0]}", **self.extra_headers()}
        data = await self._get_json(url, headers)
        return {m["id"] for m in data.get("data") or [] if m.get("id")}

    @staticmethod
    def suggest(available: set[str]) -> list[str]:
        # ตัดโมเดลเสียง/ตัวกรองออก (ใช้ตอบแชตไม่ได้) และ OpenRouter แสดงเฉพาะตัวฟรี
        skip = ("whisper", "tts", "guard", "embed", "playai", "orpheus")
        names = [m for m in available if not any(k in m for k in skip)]
        free = [m for m in names if m.endswith(":free")]
        return sorted(free or names)[:10]

    async def _generate(
        self,
        model: str,
        api_key: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
        on_delta: OnDelta | None,
    ) -> AIResult:
        messages: list[dict] = []
        messages.append({"role": "system", "content": self.build_system_prompt()})
        messages += [{"role": m.role, "content": m.content} for m in history]
        # PDF แบบภาพสแกนส่งได้เฉพาะ Gemini — เจ้าอื่นส่งเฉพาะรูป
        if any(not img.mime_type.startswith("image/") for img in images):
            prompt += "\n\n(มีไฟล์ PDF แนบมาแต่โมเดลนี้อ่านไฟล์ PDF แบบภาพสแกนไม่ได้)"
            images = [img for img in images if img.mime_type.startswith("image/")]
        if images:
            # ต้องใช้โมเดลที่รองรับรูป (vision) ไม่งั้น API จะตอบ error กลับมา
            content: str | list[dict] = [{"type": "text", "text": prompt}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{img.mime_type};base64,{img.b64()}"},
                }
                for img in images
            ]
        else:
            content = prompt
        messages.append({"role": "user", "content": content})

        payload = {"model": model, "messages": messages, "temperature": self.temperature}
        headers = {"Authorization": f"Bearer {api_key}", **self.extra_headers()}
        if on_delta is None:
            data = await self._post_json(self.url, payload, headers, model)
        else:
            text = ""
            async for chunk in self._post_stream(self.url, {**payload, "stream": True}, headers, model):
                if chunk.get("error"):
                    data = chunk
                    break
                for choice in (chunk.get("choices") or [])[:1]:
                    if piece := (choice.get("delta") or {}).get("content"):
                        text += piece
                        on_delta(text)
            else:
                data = {"choices": [{"message": {"content": text}}]}

        # OpenRouter บางครั้งตอบ HTTP 200 แต่มี error อยู่ใน body
        if err := data.get("error"):
            code = err.get("code") if isinstance(err, dict) else None
            log.error("%s error body: %s", self.name, err)
            if code == 429:
                raise RateLimitError()
            raise AIError(str(err))

        try:
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise AIError("unexpected response format") from e
        if not text:
            raise AIError("empty response")
        return AIResult(text, model)


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    url = "https://api.groq.com/openai/v1/chat/completions"


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    url = "https://openrouter.ai/api/v1/chat/completions"

    def extra_headers(self) -> dict:
        # ไม่บังคับ แต่ OpenRouter แนะนำให้ระบุเพื่อแสดงชื่อแอป
        return {"X-Title": "Discord AI Bot"}


_ERROR_REASONS: list[tuple[type[AIError], str]] = [
    (RateLimitError, "เกินโควต้า 429"),
    (ServiceUnavailableError, "เซิร์ฟเวอร์ล่ม 5xx"),
    (AITimeoutError, "ตอบช้าเกินไป"),
    (ModelNotFoundError, "ไม่พบโมเดล 404"),
    (AuthError, "API key ใช้ไม่ได้"),
    (BadRequestError, "คำขอไม่ถูกต้อง 400 (เช่น โมเดลอ่านรูปไม่ได้)"),
]


def error_reason(error: AIError) -> str:
    return next((text for cls, text in _ERROR_REASONS if isinstance(error, cls)), "error")


# แจ้งการเปลี่ยนสถานะของ AI ออกไปข้างนอก (ห้อง log ของแอดมิน): (ชนิด, หัวข้อ, รายละเอียด)
# ชนิด: "down" ใช้ไม่ได้ · "up" กลับมาใช้ได้ · "outage" ใช้ไม่ได้ทุกตัว · "recovered" กลับมาตอบได้
EventHook = Callable[[str, str, str], None]


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} วินาที"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ชม. {minutes} นาที" if minutes else f"{hours} ชม."
    return f"{minutes} นาที"


@dataclass
class _Health:
    down_since: float | None = None
    reason: str = ""
    covered: int = 0  # จำนวนคำตอบที่เจ้าอื่นตอบแทนระหว่างที่เจ้านี้ใช้ไม่ได้


class BackupProvider:
    """ใช้ตัวหลักก่อน ถ้าเกินโควต้า/ล่ม/ช้า/error ค่อยไล่ลองตัวสำรองตามลำดับ

    - เจ้าที่โดน 429 จะถูกพักไว้ชั่วครู่ คำถามช่วงนั้นข้ามไปเจ้าถัดไปเลย (ไม่ต้องรอ error ทุกครั้ง)
    - ข้อความธรรมดา: ไม่ลองซ้ำเมื่อเซิร์ฟเวอร์ล่ม เพราะมีเจ้าถัดไปตอบแทนได้ทันที
    - ข้อความที่มีรูป: ลองซ้ำก่อน และถ้าเจ้าไหนอ่านรูปไม่ได้ (400) ก็ไปเจ้าถัดไป
    - ติดตามสถานะของแต่ละเจ้า แล้วแจ้งเฉพาะตอนเปลี่ยนสถานะ (ล่ม → กลับมา) ไม่แจ้งทุกคำถาม
    """

    # ไม่ใช้ตัวสำรองเมื่อคำถามถูกตัวกรองความปลอดภัยบล็อก
    NO_BACKUP_ERRORS = (AIBlockedError,)
    # error ที่ไม่ได้แปลว่าเจ้านั้น "ล่ม" (เช่น 400 = โมเดลอ่านรูปไม่ได้) จึงไม่เปลี่ยนสถานะ
    NOT_AN_OUTAGE = (BadRequestError,)
    MIN_PAUSE_SECONDS = 60

    def __init__(self, primary: AIProvider, backups: Sequence[AIProvider]) -> None:
        self.primary = primary
        self.backups = list(backups)
        self._paused_until: dict[str, float] = {}
        self._health: dict[str, _Health] = {p.name: _Health() for p in (primary, *backups)}
        self._outage_since: float | None = None
        self.on_event: EventHook | None = None

    @property
    def backup(self) -> AIProvider:
        return self.backups[0]

    def _emit(self, kind: str, title: str, detail: str) -> None:
        if self.on_event is not None:
            self.on_event(kind, title, detail)

    def _mark_down(self, provider: AIProvider, error: AIError) -> bool:
        """คืน True ถ้าเพิ่งเปลี่ยนจากใช้ได้ → ใช้ไม่ได้"""
        health = self._health[provider.name]
        if health.down_since is not None:
            return False
        health.down_since, health.reason, health.covered = time.monotonic(), error_reason(error), 0
        return True

    def _mark_up(self, provider: AIProvider) -> None:
        health = self._health[provider.name]
        if health.down_since is None:
            return
        duration = format_duration(time.monotonic() - health.down_since)
        covered = f" · ระหว่างนั้นตัวสำรองตอบแทน {health.covered:,} ครั้ง" if health.covered else ""
        self._emit(
            "up",
            f"🟢 {provider.name} กลับมาใช้ได้แล้ว",
            f"ใช้ไม่ได้ไป {duration} (สาเหตุ: {health.reason}){covered}",
        )
        health.down_since = None

    async def generate(
        self,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData] = (),
        on_delta: OnDelta | None = None,
        retry: bool = True,
    ) -> AIResult:
        all_providers = [self.primary, *self.backups]
        now = time.monotonic()
        ready = [p for p in all_providers if self._paused_until.get(p.name, 0) <= now]
        chain = ready or all_providers  # ถ้าพักไว้หมดทุกเจ้า ก็ลองทุกเจ้าอยู่ดี
        first_error: AIError | None = None
        newly_down: list[tuple[str, str]] = []  # (ชื่อ, สาเหตุ) ของเจ้าที่เพิ่งใช้ไม่ได้ในคำถามนี้
        failed: list[str] = [f"{p.name} (พักอยู่ เกินโควต้า)" for p in all_providers if p not in chain]
        for i, provider in enumerate(chain):
            last = i == len(chain) - 1
            try:
                result = await provider.generate(
                    history, prompt, images, on_delta, retry=retry and (last or bool(images))
                )
            except self.NO_BACKUP_ERRORS:
                raise
            except AIError as e:
                first_error = first_error or e
                failed.append(f"{provider.name} ({error_reason(e)})")
                if not isinstance(e, self.NOT_AN_OUTAGE) and self._mark_down(provider, e):
                    newly_down.append((provider.name, error_reason(e)))
                if isinstance(e, RateLimitError):
                    pause = max(e.retry_after or 0, self.MIN_PAUSE_SECONDS)
                    self._paused_until[provider.name] = time.monotonic() + pause
                    log.warning("พัก %s %.0f วินาที (เกินโควต้า)", provider.name, pause)
                else:
                    log.warning("%s ใช้ไม่ได้: %r", provider.name, e)
                continue

            self._mark_up(provider)
            if self._outage_since is not None:
                duration = format_duration(time.monotonic() - self._outage_since)
                self._emit("recovered", "✅ บอทกลับมาตอบได้แล้ว", f"ใช้ไม่ได้ทุกตัวอยู่ {duration} · ตอบด้วย **{provider.name}**")
                self._outage_since = None
            for other in all_providers:
                health = self._health[other.name]
                if other is not provider and health.down_since is not None:
                    health.covered += 1
            if newly_down:
                reasons = " · ".join(f"**{name}** ({reason})" for name, reason in newly_down)
                self._emit(
                    "down",
                    f"🔴 {', '.join(name for name, _ in newly_down)} ใช้ไม่ได้",
                    f"{reasons} → สลับไปตอบด้วย **{provider.name}** (`{result.model}`)\n"
                    "-# จะแจ้งอีกครั้งเมื่อกลับมาใช้ได้",
                )
            if provider is self.primary:
                return result
            return replace(result, backup=True)

        if self._outage_since is None:
            self._outage_since = time.monotonic()
            self._emit(
                "outage", "🔥 AI ใช้ไม่ได้ทุกตัว",
                f"{' · '.join(failed)}\nผู้ใช้จะเห็นข้อความ error จนกว่าจะมีตัวใดตัวหนึ่งกลับมา",
            )
        assert first_error is not None
        # แจ้งผู้ใช้ด้วย error ของเจ้าแรกที่ลอง (สาเหตุแรก)
        raise first_error

    async def check_models(self) -> list[str]:
        problems = await self.primary.check_models()
        for backup in self.backups:
            problems += await backup.check_models()
        return problems

    async def close(self) -> None:
        await self.primary.close()
        for backup in self.backups:
            await backup.close()


_PROVIDERS: dict[str, type[AIProvider]] = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
}


def create_provider(config: Config) -> AIProvider | BackupProvider:
    primary = _PROVIDERS[config.provider](config)
    if not config.backups:
        return primary
    backups = [
        _PROVIDERS[b.provider](
            replace(
                config,
                provider=b.provider,
                api_keys=b.api_keys,
                model=b.model,
                fallback_models=b.fallback_models,
                vision_model=b.vision_model,
            )
        )
        for b in config.backups
    ]
    return BackupProvider(primary, backups)
