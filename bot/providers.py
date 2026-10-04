"""ตัวเชื่อมต่อ AI API (async ทั้งหมดด้วย aiohttp)

- Gemini  : Google Generative Language REST API
- Groq    : OpenAI-compatible API
- OpenRouter : OpenAI-compatible API (ใช้โมเดลที่ลงท้าย :free)

เพิ่มผู้ให้บริการใหม่ได้โดยสืบทอด AIProvider แล้วเพิ่มใน create_provider()
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import aiohttp

from .config import Config
from .memory import ChatMessage

log = logging.getLogger(__name__)


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
        self.temperature = config.temperature
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

    async def generate(self, history: list[ChatMessage], prompt: str) -> str:
        """รับประวัติบทสนทนา + คำถามใหม่ คืนข้อความคำตอบ

        - เซิร์ฟเวอร์ล่มชั่วคราว (5xx): ลองซ้ำกับโมเดลเดิม โดยรอนานขึ้นเรื่อย ๆ (1, 2, 4 ... วินาที)
        - ล่มต่อเนื่อง / เกินโควต้า (429) / ไม่พบโมเดล (404): ข้ามไปโมเดลสำรองถัดไป
        - error อื่น (เช่น key ผิด, ถูกบล็อก): หยุดทันที เพราะลองใหม่ก็ไม่ช่วย
        ถ้าทุกโมเดลล้มเหลว จะ raise error ล่าสุดเพื่อให้บอทแจ้งผู้ใช้
        """
        last_error: AIError | None = None
        for model in self.models:
            for attempt in range(self.max_retries + 1):
                try:
                    return await self._generate(model, history, prompt)
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
    async def _generate(self, model: str, history: list[ChatMessage], prompt: str) -> str:
        """เรียก API หนึ่งครั้งด้วยโมเดลที่กำหนด คืนข้อความคำตอบ"""


class GeminiProvider(AIProvider):
    name = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    async def _generate(self, model: str, history: list[ChatMessage], prompt: str) -> str:
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in history
        ]
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": self.temperature},
        }
        if self.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": self.system_prompt}]}

        data = await self._post_json(
            f"{self.BASE_URL}/{model}:generateContent",
            payload,
            # ส่ง key ทาง header แทน query string เพื่อไม่ให้ key หลุดไปใน log ของ URL
            {"x-goog-api-key": self.api_key},
            model,
        )

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
        return text


class OpenAICompatibleProvider(AIProvider):
    """ใช้ได้กับทุก API ที่รองรับรูปแบบ OpenAI Chat Completions (Groq, OpenRouter ฯลฯ)"""

    url: str = ""

    def extra_headers(self) -> dict:
        return {}

    async def _generate(self, model: str, history: list[ChatMessage], prompt: str) -> str:
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages += [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": prompt})

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
        return text


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    url = "https://api.groq.com/openai/v1/chat/completions"


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    url = "https://openrouter.ai/api/v1/chat/completions"

    def extra_headers(self) -> dict:
        # ไม่บังคับ แต่ OpenRouter แนะนำให้ระบุเพื่อแสดงชื่อแอป
        return {"X-Title": "Discord AI Bot"}


_PROVIDERS: dict[str, type[AIProvider]] = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
}


def create_provider(config: Config) -> AIProvider:
    return _PROVIDERS[config.provider](config)
