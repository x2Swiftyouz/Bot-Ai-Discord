"""อ่านค่าตั้งค่าทั้งหมดจาก .env — ไม่มีการ hardcode ความลับหรือชื่อโมเดล"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .utils import looks_like_secret

load_dotenv()

SUPPORTED_PROVIDERS = ("gemini", "groq", "openrouter")


class ConfigError(Exception):
    pass


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class ProviderSettings:
    """ค่าตั้งค่าของผู้ให้บริการ AI หนึ่งเจ้า (ใช้กับตัวสำรอง)"""

    provider: str
    api_keys: tuple[str, ...]
    model: str
    fallback_models: tuple[str, ...]
    vision_model: str


def _provider_settings(
    provider: str, var: str, warnings: list[str]
) -> ProviderSettings:
    """อ่าน API key ทั้งหมด, โมเดล, โมเดลสำรอง, โมเดลอ่านรูป ของผู้ให้บริการ AI จาก .env

    key หลายตัว: ใส่ใน <PREFIX>_API_KEYS คั่นด้วยจุลภาค (ใช้ตัวแรกก่อน เกินโควต้าค่อยสลับ)
    ถ้าเผลอใส่ key ไว้ใน <PREFIX>_FALLBACK_MODELS จะย้ายไปเป็น key สำรองให้อัตโนมัติ
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"{var}={provider!r} ไม่รองรับ ใช้ได้: {', '.join(SUPPORTED_PROVIDERS)}")
    prefix = provider.upper()
    keys = _split(os.getenv(f"{prefix}_API_KEY", "")) + _split(os.getenv(f"{prefix}_API_KEYS", ""))
    model = os.getenv(f"{prefix}_MODEL", "").strip()

    fallback_models: list[str] = []
    for item in _split(os.getenv(f"{prefix}_FALLBACK_MODELS", "")):
        if looks_like_secret(item):
            keys.append(item)
            warnings.append(
                f"{prefix}_FALLBACK_MODELS มี API key ปนอยู่ (ช่องนี้ต้องเป็นชื่อโมเดล) "
                f"— ย้ายไปใช้เป็น key สำรองให้แล้ว ควรย้ายไปไว้ที่ {prefix}_API_KEYS แทน"
            )
        else:
            fallback_models.append(item)

    if not keys:
        raise ConfigError(f"ไม่พบ {prefix}_API_KEY ใน .env (จำเป็นเพราะตั้ง {var}={provider})")
    if not model:
        raise ConfigError(f"ไม่พบ {prefix}_MODEL ใน .env (จำเป็นเพราะตั้ง {var}={provider})")
    if looks_like_secret(model):
        raise ConfigError(f"{prefix}_MODEL ต้องเป็นชื่อโมเดล ไม่ใช่ API key")
    # โมเดลที่ใช้เมื่อคำถามมีรูป (ไม่ใส่ = ใช้โมเดลปกติ) เช่น โมเดลฟรีของ OpenRouter ที่อ่านรูปได้
    vision_model = os.getenv(f"{prefix}_VISION_MODEL", "").strip()
    return ProviderSettings(
        provider, tuple(dict.fromkeys(keys)), model, tuple(fallback_models), vision_model
    )


def _has_provider(provider: str) -> bool:
    prefix = provider.upper()
    has_key = os.getenv(f"{prefix}_API_KEY", "").strip() or os.getenv(f"{prefix}_API_KEYS", "").strip()
    return bool(has_key and os.getenv(f"{prefix}_MODEL", "").strip())


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} ต้องเป็นตัวเลขจำนวนเต็ม (ได้ค่า {raw!r})") from e


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} ต้องเป็น true หรือ false (ได้ค่า {raw!r})")


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
    log_channel_id: int | None
    data_dir: Path
    provider: str
    api_keys: tuple[str, ...]
    model: str
    fallback_models: tuple[str, ...]
    vision_model: str
    # ผู้ให้บริการสำรอง เรียงตามลำดับที่จะลอง เมื่อตัวหลักเกินโควต้า/ล่ม (ว่าง = ไม่มี)
    backups: tuple[ProviderSettings, ...]
    max_retries: int
    warnings: tuple[str, ...]  # ข้อความเตือนเรื่องการตั้งค่า ให้ main แสดงตอนเริ่มบอท
    system_prompt: str
    timezone: str
    web_search: bool
    tavily_api_key: str
    memory_persist: bool
    daily_limit: int
    show_footer: bool
    streaming: bool
    memory_size: int
    max_images: int
    max_image_bytes: int
    max_file_bytes: int
    max_file_chars: int
    long_answer_file_chars: int
    thread_auto_title: bool
    # ถอดเสียง (Groq Whisper): ใช้ GROQ_API_KEY(S) — ว่าง = ปิด
    voice_api_keys: tuple[str, ...]
    voice_model: str
    max_audio_bytes: int
    user_cooldown: float
    ai_timeout: float
    temperature: float

    @property
    def backup_provider(self) -> str | None:
        """ชื่อผู้ให้บริการสำรองทั้งหมด (ไว้แสดงผล) เช่น "groq, openrouter" """
        return ", ".join(b.provider for b in self.backups) or None

    @property
    def backup_summary(self) -> str:
        return ", ".join(f"{b.provider}/{b.model}" for b in self.backups) or "-"

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("ไม่พบ DISCORD_TOKEN ใน .env")

        provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
        warnings: list[str] = []
        primary = _provider_settings(provider, "AI_PROVIDER", warnings)

        # BACKUP_PROVIDER: ใส่ได้หลายเจ้า คั่นด้วยจุลภาค (ลองตามลำดับ) เช่น groq,openrouter
        # ว่าง/auto = ใช้ทุกเจ้าที่กรอก key + โมเดลไว้, none = ไม่ใช้ตัวสำรอง
        backup_raw = os.getenv("BACKUP_PROVIDER", "").strip().lower()
        if backup_raw in ("", "auto"):
            backup_names = [p for p in SUPPORTED_PROVIDERS if p != provider and _has_provider(p)]
            if backup_names:
                warnings.append(
                    f"ใช้ {', '.join(backup_names)} เป็นตัวสำรองอัตโนมัติ (ปิดได้ด้วย BACKUP_PROVIDER=none)"
                )
        elif backup_raw in ("none", "off", "false"):
            backup_names = []
        else:
            backup_names = list(dict.fromkeys(_split(backup_raw)))
        if provider in backup_names:
            raise ConfigError("BACKUP_PROVIDER ต้องไม่ซ้ำกับ AI_PROVIDER")
        backups = tuple(_provider_settings(b, "BACKUP_PROVIDER", warnings) for b in backup_names)

        try:
            ai_channel_ids = tuple(
                int(x) for x in os.getenv("AI_CHANNEL_IDS", "").replace(" ", "").split(",") if x
            )
        except ValueError as e:
            raise ConfigError("AI_CHANNEL_IDS ต้องเป็นตัวเลข ID ช่อง คั่นด้วยจุลภาค") from e

        timezone = os.getenv("TIMEZONE", "").strip() or "Asia/Bangkok"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(
                f"TIMEZONE={timezone!r} ไม่ถูกต้อง (ตัวอย่าง: Asia/Bangkok) "
                "ถ้าใช้ Windows ให้รัน pip install -r requirements.txt ใหม่"
            ) from e

        guild_raw = os.getenv("GUILD_ID", "").strip()
        guild_id = _get_int("GUILD_ID", 0) if guild_raw else None

        return cls(
            discord_token=token,
            guild_id=guild_id,
            ai_channel_ids=ai_channel_ids,
            log_channel_id=_get_int("LOG_CHANNEL_ID", 0) or None,
            data_dir=Path(os.getenv("DATA_DIR", "data").strip() or "data"),
            provider=provider,
            api_keys=primary.api_keys,
            model=primary.model,
            fallback_models=primary.fallback_models,
            vision_model=primary.vision_model,
            backups=backups,
            max_retries=min(5, max(0, _get_int("AI_MAX_RETRIES", 2))),
            warnings=tuple(warnings),
            system_prompt=os.getenv(
                "SYSTEM_PROMPT", "You are a helpful assistant on Discord."
            ).strip(),
            timezone=timezone,
            web_search=_get_bool("WEB_SEARCH", True),
            tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
            memory_persist=_get_bool("MEMORY_PERSIST", True),
            daily_limit=max(0, _get_int("DAILY_LIMIT_PER_USER", 30)),
            show_footer=_get_bool("SHOW_FOOTER", True),
            streaming=_get_bool("STREAMING", True),
            memory_size=max(1, _get_int("MEMORY_SIZE", 10)),
            max_images=max(0, _get_int("MAX_IMAGES", 4)),
            max_image_bytes=int(max(0.1, _get_float("MAX_IMAGE_MB", 5)) * 1024 * 1024),
            max_file_bytes=int(max(0.1, _get_float("MAX_FILE_MB", 10)) * 1024 * 1024),
            max_file_chars=max(0, _get_int("MAX_FILE_CHARS", 40000)),
            long_answer_file_chars=max(0, _get_int("LONG_ANSWER_FILE_CHARS", 4000)),
            thread_auto_title=_get_bool("THREAD_AUTO_TITLE", True),
            voice_api_keys=tuple(dict.fromkeys(
                k for k in _split(os.getenv("GROQ_API_KEY", "")) + _split(os.getenv("GROQ_API_KEYS", ""))
                + [x for x in _split(os.getenv("GROQ_FALLBACK_MODELS", "")) if looks_like_secret(x)]
            )) if _get_bool("VOICE_TRANSCRIPTION", True) else (),
            voice_model=os.getenv("VOICE_MODEL", "").strip() or "whisper-large-v3-turbo",
            # Groq รับไฟล์เสียงฟรีไม่เกิน 25 MB
            max_audio_bytes=int(min(25, max(0.1, _get_float("MAX_AUDIO_MB", 25))) * 1024 * 1024),
            user_cooldown=max(0.0, _get_float("USER_COOLDOWN_SECONDS", 10)),
            ai_timeout=max(5.0, _get_float("AI_TIMEOUT_SECONDS", 60)),
            temperature=_get_float("AI_TEMPERATURE", 0.7),
        )
