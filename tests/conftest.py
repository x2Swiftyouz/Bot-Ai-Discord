"""ตั้งค่าร่วมของเทสต์: ปิดการอ่าน .env จริง และเตรียม environment ที่สะอาดให้ทุกเทสต์"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import dotenv
import pytest

# ห้ามเทสต์อ่านไฟล์ .env จริงของเครื่อง (มี key จริง และทำให้ผลเทสต์เปลี่ยนตามเครื่อง)
dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV_PREFIXES = (
    "DISCORD_", "GUILD_", "AI_", "BACKUP_", "GEMINI_", "GROQ_", "OPENROUTER_", "TAVILY_", "LOG_",
    "SYSTEM_", "TIMEZONE", "WEB_SEARCH", "SHOW_FOOTER", "STREAMING", "MEMORY_", "DAILY_", "MAX_",
    "USER_", "DATA_DIR", "LONG_", "THREAD_", "VOICE_",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """environment ขั้นต่ำที่ใช้ได้ (Gemini อย่างเดียว) — แต่ละเทสต์ set เพิ่มเองได้"""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    values = {
        "DISCORD_TOKEN": "test-token",
        "GEMINI_API_KEY": "gemini-key",
        "GEMINI_MODEL": "gemini-test",
        "DATA_DIR": str(tmp_path / "data"),
        "USER_COOLDOWN_SECONDS": "0",
        "STREAMING": "false",
        "BACKUP_PROVIDER": "none",
        "VOICE_TRANSCRIPTION": "false",
        "AI_MAX_RETRIES": "0",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    def set_env(**kwargs: str) -> None:
        for key, value in kwargs.items():
            monkeypatch.setenv(key, value)

    return set_env


def run(coro):
    return asyncio.run(coro)


@contextlib.asynccontextmanager
async def serve(*routes):
    """เซิร์ฟเวอร์ HTTP จำลอง: serve(("POST", "/path", handler), ...) → yield base URL"""
    from aiohttp import web

    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()
