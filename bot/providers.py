"""ตัวเชื่อมต่อ AI API (async ทั้งหมดด้วย aiohttp)

- Gemini  : Google Generative Language REST API
- Groq    : OpenAI-compatible API
- OpenRouter : OpenAI-compatible API (ใช้โมเดลที่ลงท้าย :free)

เพิ่มผู้ให้บริการใหม่ได้โดยสืบทอด AIProvider แล้วเพิ่มใน create_provider()
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
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

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after
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
        self.api_key = config.api_key
        self.model = config.model
        # ลองโมเดลหลักก่อน ถ้าล่ม/เกินโควต้า/ถูกถอด ค่อยไล่ลองโมเดลสำรองตามลำดับ
        self.models = list(dict.fromkeys((config.model, *config.fallback_models)))
        self.max_retries = config.max_retries
        self.system_prompt = config.system_prompt
        self.timezone = config.timezone
        self.temperature = config.temperature
        self.web_search = config.web_search
        self._timeout = aiohttp.ClientTimeout(total=config.ai_timeout)
        self._session: aiohttp.ClientSession | None = None

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
                if resp.status == 429:
                    # ข้อความใน body บอกว่าโควต้าตัวไหนหมด (ต่อนาที / ต่อวัน / ค้นเว็บ)
                    log.warning(
                        "%s HTTP 429 (model %s): %s", self.name, model, (await resp.text())[:800]
                    )
                    raise RateLimitError(_parse_retry_after(resp.headers))
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
                return await resp.json(content_type=None)
        except TimeoutError as e:  # aiohttp ใช้ asyncio.TimeoutError (= TimeoutError ใน 3.11+)
            raise AITimeoutError("timeout") from e
        except aiohttp.ClientError as e:
            log.error("%s connection error: %r", self.name, e)
            raise AIError("connection error") from e

    def build_system_prompt(self) -> str:
        """system prompt + วันเวลาปัจจุบัน (AI ไม่รู้วันที่เองจึงคำนวณระยะเวลาผิดถ้าไม่บอก)"""
        now = f"ข้อมูลอ้างอิง: ตอนนี้คือ{now_text(self.timezone)} ใช้ข้อมูลนี้เมื่อต้องคำนวณวันเวลา"
        return f"{self.system_prompt}\n\n{now}" if self.system_prompt else now

    async def generate(
        self, history: list[ChatMessage], prompt: str, images: Sequence[ImageData] = ()
    ) -> AIResult:
        """รับประวัติบทสนทนา + คำถามใหม่ (+ รูปถ้ามี) คืนข้อความคำตอบ

        - เซิร์ฟเวอร์ล่มชั่วคราว (5xx): ลองซ้ำกับโมเดลเดิม โดยรอนานขึ้นเรื่อย ๆ (1, 2, 4 ... วินาที)
        - ล่มต่อเนื่อง / เกินโควต้า (429) / ไม่พบโมเดล (404): ข้ามไปโมเดลสำรองถัดไป
        - error อื่น (เช่น key ผิด, ถูกบล็อก): หยุดทันที เพราะลองใหม่ก็ไม่ช่วย
        ถ้าทุกโมเดลล้มเหลว จะ raise error ล่าสุดเพื่อให้บอทแจ้งผู้ใช้
        """
        last_error: AIError | None = None
        for model in self.models:
            for attempt in range(self.max_retries + 1):
                try:
                    return await self._generate(model, history, prompt, images)
                except ServiceUnavailableError as e:
                    last_error = e
                    if attempt < self.max_retries:
                        delay = 2**attempt
                        log.info("%s %s busy, retrying in %ss", self.name, model, delay)
                        await asyncio.sleep(delay)
                except (RateLimitError, ModelNotFoundError) as e:
                    last_error = e
                    break
            if model != self.models[-1]:
                log.warning("%s model %s failed (%r), trying fallback", self.name, model, last_error)
        assert last_error is not None
        raise last_error

    @abstractmethod
    async def _generate(
        self,
        model: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
    ) -> AIResult:
        """เรียก API หนึ่งครั้งด้วยโมเดลที่กำหนด คืนข้อความคำตอบ"""


class GeminiProvider(AIProvider):
    name = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    # โดน 429 ตอนค้นเว็บ → พักการค้นเว็บของโมเดลนั้นกี่วินาที
    SEARCH_PAUSE_SECONDS = 30 * 60

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        # โมเดลที่ใช้ค้นเว็บไม่ได้ (ตอบ 400) จะไม่ขอค้นเว็บอีกจนกว่าจะรีสตาร์ท
        self._no_search_models: set[str] = set()
        # โมเดลที่โควต้าค้นเว็บหมด: model -> เวลาที่จะลองค้นเว็บได้อีก
        self._search_paused_until: dict[str, float] = {}

    def _search_enabled(self, model: str) -> bool:
        if not self.web_search or model in self._no_search_models:
            return False
        return time.monotonic() >= self._search_paused_until.get(model, 0)

    async def _generate(
        self,
        model: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
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

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self.temperature},
            "systemInstruction": {"parts": [{"text": self.build_system_prompt()}]},
        }
        search = self._search_enabled(model)
        if search:
            # ให้ Gemini ตัดสินใจเองว่าจะค้น Google ไหม (ค้นเฉพาะคำถามที่ต้องใช้ข้อมูลล่าสุด)
            payload["tools"] = [{"google_search": {}}]

        url = f"{self.BASE_URL}/{model}:generateContent"
        # ส่ง key ทาง header แทน query string เพื่อไม่ให้ key หลุดไปใน log ของ URL
        headers = {"x-goog-api-key": self.api_key}
        try:
            data = await self._post_json(url, payload, headers, model)
        except BadRequestError:
            if not search:
                raise
            log.warning("Gemini %s ใช้ค้นเว็บไม่ได้ จะตอบแบบไม่ค้นเว็บแทน", model)
            self._no_search_models.add(model)
            payload.pop("tools")
            data = await self._post_json(url, payload, headers, model)
        except RateLimitError:
            if not search:
                raise
            # โควต้าค้นเว็บมักหมดก่อนโควต้าปกติ ลองตอบแบบไม่ค้นเว็บอีกครั้ง
            # ถ้ายังโดน 429 แปลว่าโควต้าปกติหมดด้วย ให้ error ส่งต่อไปตามปกติ
            minutes = self.SEARCH_PAUSE_SECONDS // 60
            log.warning("Gemini %s โดน 429 ตอนค้นเว็บ พักการค้นเว็บ %s นาที", model, minutes)
            self._search_paused_until[model] = time.monotonic() + self.SEARCH_PAUSE_SECONDS
            payload.pop("tools")
            data = await self._post_json(url, payload, headers, model)

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
        sources: list[tuple[str, str]] = []
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            if (uri := web.get("uri")) and all(uri != u for _, u in sources):
                sources.append((web.get("title") or "ลิงก์", uri))
        return AIResult(
            text, model, tuple(sources), searched=bool(grounding.get("webSearchQueries"))
        )


class OpenAICompatibleProvider(AIProvider):
    """ใช้ได้กับทุก API ที่รองรับรูปแบบ OpenAI Chat Completions (Groq, OpenRouter ฯลฯ)"""

    url: str = ""

    def extra_headers(self) -> dict:
        return {}

    async def _generate(
        self,
        model: str,
        history: list[ChatMessage],
        prompt: str,
        images: Sequence[ImageData],
    ) -> AIResult:
        messages: list[dict] = []
        messages.append({"role": "system", "content": self.build_system_prompt()})
        messages += [{"role": m.role, "content": m.content} for m in history]
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

        data = await self._post_json(
            self.url,
            {"model": model, "messages": messages, "temperature": self.temperature},
            {"Authorization": f"Bearer {self.api_key}", **self.extra_headers()},
            model,
        )

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


class BackupProvider:
    """ใช้ตัวหลักก่อน ถ้าเกินโควต้า/ล่ม/ช้า/error ค่อยใช้ตัวสำรอง

    ถ้าตัวหลักโดน 429 จะพักตัวหลักไว้ชั่วครู่ แล้วส่งไปตัวสำรองตรง ๆ (ไม่ต้องรอตัวหลักตอบ error ทุกครั้ง)
    """

    # ไม่ใช้ตัวสำรองเมื่อคำถามถูกตัวกรองความปลอดภัยบล็อก
    NO_BACKUP_ERRORS = (AIBlockedError,)
    MIN_PAUSE_SECONDS = 60

    def __init__(self, primary: AIProvider, backup: AIProvider) -> None:
        self.primary = primary
        self.backup = backup
        self._primary_paused_until = 0.0

    async def generate(
        self, history: list[ChatMessage], prompt: str, images: Sequence[ImageData] = ()
    ) -> AIResult:
        primary_error: AIError | None = None
        if time.monotonic() >= self._primary_paused_until:
            try:
                return await self.primary.generate(history, prompt, images)
            except self.NO_BACKUP_ERRORS:
                raise
            except AIError as e:
                primary_error = e
                if isinstance(e, RateLimitError):
                    pause = max(e.retry_after or 0, self.MIN_PAUSE_SECONDS)
                    self._primary_paused_until = time.monotonic() + pause
                    log.warning("พัก %s %.0f วินาที (เกินโควต้า) ใช้ %s แทน",
                                self.primary.name, pause, self.backup.name)
                else:
                    log.warning("%s ใช้ไม่ได้ (%r) ใช้ %s แทน", self.primary.name, e, self.backup.name)
        try:
            result = await self.backup.generate(history, prompt, images)
        except AIError as e:
            log.warning("ตัวสำรอง %s ก็ใช้ไม่ได้: %r", self.backup.name, e)
            # แจ้งผู้ใช้ด้วย error ของตัวหลัก (สาเหตุแรก) ถ้ามี
            raise primary_error or e from e
        return AIResult(result.text, result.model, result.sources, result.searched, backup=True)

    async def close(self) -> None:
        await self.primary.close()
        await self.backup.close()


_PROVIDERS: dict[str, type[AIProvider]] = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
}


def create_provider(config: Config) -> AIProvider | BackupProvider:
    primary = _PROVIDERS[config.provider](config)
    if not config.backup_provider:
        return primary
    backup_config = replace(
        config,
        provider=config.backup_provider,
        api_key=config.backup_api_key,
        model=config.backup_model,
        fallback_models=config.backup_fallback_models,
    )
    return BackupProvider(primary, _PROVIDERS[config.backup_provider](backup_config))
