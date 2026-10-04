"""อ่านค่าตั้งค่าทั้งหมดจาก .env — ไม่มีการ hardcode ความลับหรือชื่อโมเดล"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SUPPORTED_PROVIDERS = ("gemini", "groq", "openrouter")


class ConfigError(Exception):
    pass


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} ต้องเป็นตัวเลขจำนวนเต็ม (ได้ค่า {raw!r})") from e


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} ต้องเป็นตัวเลข (ได้ค่า {raw!r})") from e


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int | None
    ai_channel_ids: tuple[int, ...]
    data_dir: Path
    provider: str
    api_key: str
    model: str
    fallback_models: tuple[str, ...]
    max_retries: int
    system_prompt: str
    memory_size: int
    user_cooldown: float
    ai_timeout: float
    temperature: float

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("ไม่พบ DISCORD_TOKEN ใน .env")

        provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ConfigError(
                f"AI_PROVIDER={provider!r} ไม่รองรับ ใช้ได้: {', '.join(SUPPORTED_PROVIDERS)}"
            )

        prefix = provider.upper()
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        model = os.getenv(f"{prefix}_MODEL", "").strip()
        if not api_key:
            raise ConfigError(f"ไม่พบ {prefix}_API_KEY ใน .env")
        if not model:
            raise ConfigError(f"ไม่พบ {prefix}_MODEL ใน .env")

        fallback_models = tuple(
            m.strip() for m in os.getenv(f"{prefix}_FALLBACK_MODELS", "").split(",") if m.strip()
        )

        try:
            ai_channel_ids = tuple(
                int(x) for x in os.getenv("AI_CHANNEL_IDS", "").replace(" ", "").split(",") if x
            )
        except ValueError as e:
            raise ConfigError("AI_CHANNEL_IDS ต้องเป็นตัวเลข ID ช่อง คั่นด้วยจุลภาค") from e

        guild_raw = os.getenv("GUILD_ID", "").strip()
        guild_id = _get_int("GUILD_ID", 0) if guild_raw else None

        return cls(
            discord_token=token,
            guild_id=guild_id,
            ai_channel_ids=ai_channel_ids,
            data_dir=Path(os.getenv("DATA_DIR", "data").strip() or "data"),
            provider=provider,
            api_key=api_key,
            model=model,
            fallback_models=fallback_models,
            max_retries=min(5, max(0, _get_int("AI_MAX_RETRIES", 2))),
            system_prompt=os.getenv(
                "SYSTEM_PROMPT", "You are a helpful assistant on Discord."
            ).strip(),
            memory_size=max(1, _get_int("MEMORY_SIZE", 10)),
            user_cooldown=max(0.0, _get_float("USER_COOLDOWN_SECONDS", 10)),
            ai_timeout=max(5.0, _get_float("AI_TIMEOUT_SECONDS", 60)),
            temperature=_get_float("AI_TEMPERATURE", 0.7),
        )
