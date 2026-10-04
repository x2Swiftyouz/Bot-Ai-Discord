"""Discord AI Chatbot — จุดเริ่มต้นโปรแกรม

รัน: python main.py
"""

from __future__ import annotations

import asyncio
import io
import itertools
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from bot import persona
from bot.adminlog import AdminLog
from bot.channels import AIChannelStore
from bot.config import Config, ConfigError
from bot.cooldown import UserCooldown
from bot.media import is_document, is_image, read_documents, read_images
from bot.memory import ChannelMemory
from bot.providers import (
    CURRENT_PERSONA, CURRENT_USER_NOTES, AIError, AIProvider, BackupProvider, ImageData, OnDelta, create_provider,
)
from bot.search import SearchError, TavilySearch, format_results, should_search
from bot.storage import Database
from bot.streaming import StreamPreview
from bot.utils import now_text, redact, split_message
from bot.views import AnswerContext, AnswerView, CloseThreadView

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")


class _RedactSecrets(logging.Filter):
    """ซ่อน API key / token ที่อาจหลุดมาในข้อความ log (เช่น ข้อความ error จาก API)"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactSecrets())

# ห้ามคำตอบของ AI ไป ping @everyone / @here / role / ผู้ใช้คนอื่น
SAFE_MENTIONS = discord.AllowedMentions.none()
# ตอนตอบกลับข้อความ ให้แจ้งเตือนเฉพาะคนที่ถาม
REPLY_MENTIONS = discord.AllowedMentions(
    everyone=False, users=False, roles=False, replied_user=True
)

# ข้อความในห้องคุยกับ AI ที่ขึ้นต้นด้วยสิ่งนี้ บอทจะไม่ตอบ (ไว้คุยกันเอง)
IGNORE_PREFIX = "//"
IMAGE_ONLY_QUESTION = "ช่วยอธิบายรูปนี้หน่อย"
FILE_ONLY_QUESTION = "ช่วยสรุปไฟล์นี้หน่อย"
REPLY_ONLY_QUESTION = "ช่วยอธิบายหรือตอบข้อความนี้หน่อย"
MAX_SOURCES = 3
CONTINUE_QUESTION = "เขียนต่อจากคำตอบก่อนหน้าให้จบ ต่อจากจุดที่ค้างไว้เลย ไม่ต้องทวนซ้ำ"
# ปุ่มคำถามแนะนำ: ชนิด -> (ป้ายบอกในคำตอบ, คำสั่งให้ AI) — ส่งคำตอบเดิมไปด้วย ไม่พึ่งความจำของห้อง
FOLLOWUPS = {
    "shorter": ("📝 สั้นลง", "สรุปข้อความด้านล่างให้สั้นลงมาก เหลือแต่ใจความสำคัญ คงภาษาเดิม"),
    "detail": ("📖 ละเอียดขึ้น", "อธิบายข้อความด้านล่างให้ละเอียดขึ้น เพิ่มตัวอย่างหรือรายละเอียดที่เป็นประโยชน์ คงภาษาเดิม"),
    "to_english": ("🌐 แปลอังกฤษ", "แปลข้อความด้านล่างเป็นภาษาอังกฤษให้เป็นธรรมชาติ ตอบเฉพาะคำแปล"),
    "to_thai": ("🌐 แปลไทย", "แปลข้อความด้านล่างเป็นภาษาไทยให้เป็นธรรมชาติ ตอบเฉพาะคำแปล"),
}
# เมนูคลิกขวาที่ข้อความ: ชื่อเมนู -> (ป้ายบอกในผลลัพธ์, คำสั่งให้ AI)
MESSAGE_ACTIONS = {
    "translate": (
        "🌐 แปล",
        "แปลข้อความด้านล่างเป็นภาษาไทยให้เป็นธรรมชาติ ถ้าเป็นภาษาไทยอยู่แล้วให้แปลเป็นภาษาอังกฤษ "
        "ตอบเฉพาะคำแปล ไม่ต้องอธิบาย ถ้ามีรูปที่มีตัวหนังสือให้แปลตัวหนังสือในรูปด้วย",
    ),
    "summarize": (
        "📝 สรุป",
        "สรุปใจความสำคัญของข้อความด้านล่างเป็นภาษาไทยแบบกระชับ เป็นข้อ ๆ",
    ),
    "explain": (
        "💡 อธิบาย",
        "อธิบายข้อความด้านล่างเป็นภาษาไทยให้เข้าใจง่าย ถ้ามีศัพท์เฉพาะ คำแสลง ตัวย่อ โค้ด "
        "หรือมุก ให้อธิบายด้วย",
    ),
}
# พิมพ์ "จำไว้ว่า ..." ในห้อง AI หรือตอน mention = บันทึกข้อมูลส่วนตัว (/remember) โดยไม่ต้องถาม AI
REMEMBER_PREFIX = re.compile(r"^\s*(จำไว้ว่า|จำไว้นะว่า|ช่วยจำว่า|remember that)\s*[:：]?\s*(.+)", re.IGNORECASE | re.DOTALL)
MAX_NOTES = 10
MAX_NOTE_LENGTH = 200
REACT_THINKING = "👀"
REACT_ERROR = "⚠️"
BRAND_COLOR = discord.Color.from_rgb(88, 101, 242)

# ส่งข้อความ 1 ก้อน (พร้อมปุ่มถ้ามี) แล้วคืนข้อความที่ส่งไป
# (ข้อความ, ปุ่ม, ไฟล์แนบ) -> ข้อความที่ส่งไป
Sender = Callable[..., Awaitable["discord.Message | discord.WebhookMessage"]]
LONG_ANSWER_PREVIEW = 1500


class AIChatBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # ต้องเปิด Message Content Intent ใน Developer Portal ด้วย
        super().__init__(intents=intents, allowed_mentions=SAFE_MENTIONS)

        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.data_dir / "bot.db")
        self.memory = ChannelMemory(config.memory_size, self.db if config.memory_persist else None)
        self.search = TavilySearch(config.tavily_api_key) if config.tavily_api_key else None
        self.admin_log = AdminLog(self, config.log_channel_id)
        self.cooldown = UserCooldown(config.user_cooldown)
        self.ai_channels = AIChannelStore(
            config.ai_channel_ids, config.data_dir / "ai_channels.json"
        )
        self.ai: AIProvider | BackupProvider = create_provider(config)
        if isinstance(self.ai, BackupProvider):
            # แจ้งเข้าห้อง log เมื่อสลับไปตัวสำรอง / AI ใช้ไม่ได้ทุกตัว
            self.ai.on_event = self.admin_log.status
        self.answer_count = 0
        self._cleaned_commands = False
        self._background: set[asyncio.Task] = set()
        self._statuses = itertools.cycle(self._status_texts())
        self._register_commands()

    # ---------- lifecycle ----------

    async def setup_hook(self) -> None:
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s", len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s) (อาจใช้เวลาสักพักกว่าจะขึ้น)", len(synced))
        # ปุ่มถาวร (🔒 ปิดเธรด) ต้องลงทะเบียนทุกครั้งที่เริ่มบอท ถึงจะกดได้หลังรีสตาร์ท
        self.add_view(CloseThreadView())
        self.rotate_status.start()
        self.daily_summary.change_interval(time=dtime(0, 0, tzinfo=ZoneInfo(self.config.timezone)))
        self.daily_summary.start()

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (ID %s) | provider=%s model=%s backup=%s",
            self.user, self.user.id if self.user else "?", self.config.provider, self.config.model,
            self.config.backup_summary,
        )
        if not self._cleaned_commands:
            self._cleaned_commands = True
            await self._remove_stale_commands()
            problems = await self.ai.check_models()
            search = "Tavily" if self.config.tavily_api_key else "ปิด"
            backups = "\n".join(f"`{b.provider}/{b.model}`" for b in self.config.backups) or "ไม่มี"
            self.admin_log.status(
                "info", "🤖 บอทออนไลน์แล้ว",
                f"**AI หลัก:** `{self.config.provider}/{self.config.model}`\n"
                f"**สำรอง:**\n{backups}\n**ค้นเว็บ:** {search}",
            )
            for problem in problems:
                self.admin_log.status("warning", "⚠️ พบโมเดลที่ใช้ไม่ได้ใน .env", problem.removeprefix("⚠️ "))

    @tasks.loop(hours=24)
    async def daily_summary(self) -> None:
        """เที่ยงคืน (ตาม TIMEZONE): ส่งสรุปสถิติของเมื่อวานเข้าห้อง log"""
        yesterday = (
            datetime.now(ZoneInfo(self.config.timezone)) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        channel = self.get_channel(self.admin_log.channel_id or 0)
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        self.admin_log.post(embed=self._stats_embed(guild_id, day=yesterday))

    @daily_summary.before_loop
    async def _before_daily_summary(self) -> None:
        await self.wait_until_ready()

    async def _remove_stale_commands(self) -> None:
        """ลบคำสั่ง slash ที่ค้างอยู่ใน Discord แต่ไม่ได้มาจากโค้ดนี้

        เช่น คำสั่งจากโปรแกรมอื่นที่เคยใช้ token เดียวกัน หรือคำสั่ง global เก่าตอนยังไม่ได้ตั้ง GUILD_ID
        ซึ่งทำให้คำสั่งขึ้นซ้ำ 2 อัน — บอทนี้เป็นเจ้าของแอปทั้งหมด จึงเหลือไว้แค่คำสั่งของตัวเอง
        """
        app_id = self.application_id
        if app_id is None:
            return
        target = self.config.guild_id
        # ตั้ง GUILD_ID = คำสั่งอยู่เฉพาะเซิร์ฟเวอร์นั้น → ฝั่ง global ต้องว่าง
        scopes: list[discord.Guild | None] = [None] if target else []
        # เซิร์ฟเวอร์อื่น ๆ (หรือทุกเซิร์ฟเวอร์ถ้าใช้แบบ global) ต้องไม่มีคำสั่งระดับเซิร์ฟเวอร์ค้าง
        scopes += [g for g in self.guilds if g.id != target]
        for scope in scopes:
            where = "global" if scope is None else f"เซิร์ฟเวอร์ {scope.name}"
            try:
                stale = await self.tree.fetch_commands(guild=scope)
                if not stale:
                    continue
                if scope is None:
                    await self.http.bulk_upsert_global_commands(app_id, [])
                else:
                    await self.http.bulk_upsert_guild_commands(app_id, scope.id, [])
                log.info(
                    "ลบคำสั่งที่ค้าง (%s): %s", where, ", ".join(f"/{c.name}" for c in stale)
                )
            except discord.HTTPException as e:
                log.warning("ลบคำสั่งที่ค้าง (%s) ไม่สำเร็จ: %r", where, e)

    def _status_texts(self) -> list[Callable[[], str]]:
        texts: list[Callable[[], str]] = [
            lambda: "💬 พิมพ์คุยในห้อง AI ได้เลย",
            lambda: "❓ /ask ถามอะไรก็ได้",
            lambda: f"✨ ตอบไปแล้ว {self.answer_count:,} ครั้ง",
        ]
        if self.config.max_images:
            texts.insert(2, lambda: "📷 ส่งรูปให้ช่วยดูได้นะ")
        return texts

    @tasks.loop(seconds=30)
    async def rotate_status(self) -> None:
        """สถานะบอทหมุนเวียนทุก 30 วินาที"""
        text = next(self._statuses)()
        await self.change_presence(activity=discord.CustomActivity(name=text))

    @rotate_status.before_loop
    async def _before_rotate_status(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        await self.ai.close()
        if self.search:
            await self.search.close()
        await super().close()
        self.db.close()

    # ---------- core ----------

    def _today(self) -> str:
        return datetime.now(ZoneInfo(self.config.timezone)).strftime("%Y-%m-%d")

    async def ask_ai(self, ctx: AnswerContext, on_delta: OnDelta | None = None) -> None:
        """ส่งคำถามไปยัง AI พร้อมบริบทของช่อง (+ ผลค้นเว็บถ้าต้องใช้) แล้วเก็บผลลัพธ์ลง ctx"""
        # ใส่ชื่อผู้ถาม เพราะในช่องเดียวอาจมีหลายคนคุยกับบอท
        prompt = f"{ctx.asker_name}: {ctx.question}"
        # ความจำเก็บแค่คำถาม ไม่เก็บรูปหรือผลค้นเว็บ (ประหยัดโควต้า) จึงจดไว้ว่ามีรูปแนบ
        ctx.prompt = prompt + (f" [แนบรูป {len(ctx.images)} รูป]" if ctx.images else "")
        if ctx.file_names:
            ctx.prompt += f" [แนบไฟล์: {', '.join(ctx.file_names)}]"
        if ctx.attachments_text:
            prompt += "\n\n" + ctx.attachments_text
        ctx.footer = ""
        # บุคลิกของห้อง (/persona) — ใช้แทน SYSTEM_PROMPT ระหว่างคำถามนี้
        persona_token = CURRENT_PERSONA.set(self._persona_prompt(ctx) if ctx.use_memory else None)
        # ข้อมูลที่ผู้ถามขอให้จำไว้ (/remember)
        notes_token = CURRENT_USER_NOTES.set(self._notes_prompt(ctx) if ctx.use_memory else None)
        try:
            await self._ask_ai(ctx, prompt, on_delta)
        finally:
            CURRENT_PERSONA.reset(persona_token)
            CURRENT_USER_NOTES.reset(notes_token)

    def _notes_prompt(self, ctx: AnswerContext) -> str | None:
        notes = self.db.notes(ctx.asker_id)
        if not notes:
            return None
        lines = "\n".join(f"- {n}" for n in notes)
        return (
            f"ข้อมูลเกี่ยวกับ {ctx.asker_name} (คนที่กำลังถาม) ที่เขาขอให้คุณจำไว้ "
            f"ใช้เมื่อเกี่ยวข้องกับคำถามเท่านั้น ไม่ต้องพูดถึงทุกครั้ง:\n{lines}"
        )

    def remember(self, user_id: int, note: str) -> str:
        """บันทึกข้อมูลส่วนตัว คืนข้อความแจ้งผล"""
        note = " ".join(note.split())[:MAX_NOTE_LENGTH]
        if not note:
            return "พิมพ์สิ่งที่อยากให้จำด้วยนะ เช่น `ฉันชื่อปีเตอร์ ชอบเล่น FiveM`"
        if len(self.db.notes(user_id)) >= MAX_NOTES:
            return f"จำได้สูงสุด {MAX_NOTES} ข้อ ลบข้อเก่าด้วย `/forget` ก่อนนะ (ดูทั้งหมดด้วย `/memory`)"
        self.db.add_note(user_id, note)
        return f"📝 จำไว้แล้ว: **{note}**\n-# บอทจะจำเรื่องนี้ได้ทุกห้อง แม้ `/reset` · ดูทั้งหมด `/memory` · ลบ `/forget`"

    def _persona_prompt(self, ctx: AnswerContext) -> str | None:
        """บุคลิกของห้องนี้ (เธรดใช้บุคลิกของห้องแม่ถ้าตัวเองไม่ได้ตั้ง)"""
        for channel_id in (ctx.channel_id, getattr(ctx.channel, "parent_id", None)):
            if channel_id and (found := persona.resolve(self.db.get_setting(persona.key_for(channel_id)))):
                return found[1]
        return None

    async def _ask_ai(self, ctx: AnswerContext, prompt: str, on_delta: OnDelta | None) -> None:
        async with self.memory.lock(ctx.channel_id):
            history = self.memory.get(ctx.channel_id) if ctx.use_memory else []
            started = time.monotonic()
            sources: tuple[tuple[str, str], ...] = ()
            if self.search and ctx.search_query and should_search(ctx.search_query):
                try:
                    found = await self.search.search(ctx.search_query)
                except SearchError as e:
                    log.warning("ค้นเว็บไม่สำเร็จ ตอบแบบไม่ค้นเว็บแทน: %s", e)
                    self.admin_log.post(f"🔎 ค้นเว็บ (Tavily) ไม่สำเร็จ: `{e}`", key="tavily")
                    found = []
                if found:
                    prompt += "\n\n" + format_results(found, now_text(self.config.timezone))
                    sources = tuple((r.title, r.url) for r in found)
            try:
                result = await self.ai.generate(history, prompt, ctx.images, on_delta)
            except AIError as e:
                log.warning("AI error in channel %s: %r", ctx.channel_id, e)
                ctx.ok, ctx.answer = False, e.user_message
            except Exception as e:
                log.exception("Unexpected error while calling AI")
                self.admin_log.post(f"❌ error ไม่คาดคิด: `{e!r}`"[:500], key=f"unexpected:{type(e).__name__}")
                ctx.ok, ctx.answer = False, "⚠️ เกิดข้อผิดพลาดที่ไม่คาดคิด ลองใหม่อีกครั้งนะ"
            else:
                if sources:
                    result = replace(result, sources=sources, searched=True)
                ctx.ok, ctx.answer = True, result.text
                if ctx.use_memory:
                    self.memory.add_exchange(ctx.channel_id, ctx.prompt, result.text)
                self.answer_count += 1
        elapsed = time.monotonic() - started
        self.db.record_usage(
            day=self._today(), guild_id=ctx.guild_id, user_id=ctx.asker_id,
            user_name=ctx.asker_name, ok=ctx.ok,
            model=result.model if ctx.ok else None,
            backup=ctx.ok and result.backup, searched=ctx.ok and result.searched,
            elapsed=elapsed,
        )
        if ctx.ok:
            ctx.footer = self._footer(result, elapsed, self._remaining(ctx))

    def _remaining(self, ctx: AnswerContext) -> int | None:
        """จำนวนคำถามที่เหลือวันนี้ (None = ไม่จำกัด)"""
        if not self.config.daily_limit or ctx.exempt:
            return None
        return max(0, self.config.daily_limit - self.db.used_today(self._today(), ctx.asker_id))

    def _footer(self, result, elapsed: float, remaining: int | None = None) -> str:
        """บรรทัดตัวเล็ก (-#) ใต้คำตอบ: เวลาที่ใช้ · โมเดล · แหล่งที่มาจากการค้นเว็บ"""
        if not self.config.show_footer:
            return ""
        info = f"-# ⚡ {elapsed:.1f} วิ · {result.model}"
        if result.backup:
            info += " · 🛟 สำรอง"
        if result.searched:
            info += " · 🔎 ค้นเว็บ"
        if result.sources:
            # <ลิงก์> กันไม่ให้ Discord แสดงพรีวิวลิงก์ใหญ่ ๆ
            links = " · ".join(
                f"[{title[:40]}](<{uri}>)" for title, uri in result.sources[:MAX_SOURCES]
            )
            info += f"\n-# 📚 แหล่งที่มา: {links}"
        if remaining is not None and remaining <= 5:
            info += f"\n-# 📊 เหลือ {remaining} คำถามสำหรับวันนี้"
        return info

    async def _run(self, ctx: AnswerContext, on_delta: OnDelta | None = None) -> None:
        """ถาม AI ตามข้อมูลใน ctx แล้วเก็บผลลัพธ์กลับลง ctx"""
        await self.ask_ai(ctx, on_delta)

    async def _run_streaming(
        self, ctx: AnswerContext, start: Callable[[str], Awaitable[discord.Message | discord.WebhookMessage]]
    ) -> discord.Message | discord.WebhookMessage | None:
        """ถาม AI โดยแสดงคำตอบค่อย ๆ พิมพ์ (ถ้าเปิด STREAMING) คืนข้อความ preview ที่ส่งไปแล้ว (ถ้ามี)"""
        if not self.config.streaming:
            await self._run(ctx)
            return None
        preview = StreamPreview(start, header=ctx.header)
        try:
            await self._run(ctx, preview.update)
        finally:
            existing = await preview.finish()
        return existing

    def _render(
        self, ctx: AnswerContext, allow_file: bool = True
    ) -> tuple[list[str], discord.File | None]:
        """แบ่งคำตอบเป็นข้อความ ถ้ายาวมาก (LONG_ANSWER_FILE_CHARS) แสดงส่วนต้น + แนบฉบับเต็มเป็นไฟล์ .txt"""
        footer = f"\n{ctx.footer}" if ctx.footer else ""
        limit = self.config.long_answer_file_chars
        if allow_file and ctx.ok and limit and len(ctx.answer) > limit:
            preview = split_message(ctx.answer, LONG_ANSWER_PREVIEW)[0]
            text = (
                f"{ctx.header}{preview}\n…\n"
                f"-# 📄 คำตอบยาว {len(ctx.answer):,} ตัวอักษร — ฉบับเต็มอยู่ในไฟล์แนบ{footer}"
            )
            file = discord.File(io.BytesIO(ctx.answer.encode("utf-8")), filename="answer.txt")
            return split_message(text), file
        return split_message(ctx.header + ctx.answer + footer) or ["(AI ไม่ได้ส่งข้อความกลับมา)"], None

    def _chunks(self, ctx: AnswerContext) -> list[str]:
        return self._render(ctx)[0]

    async def _deliver(
        self,
        ctx: AnswerContext,
        send: Sender,
        existing: discord.Message | discord.WebhookMessage | None = None,
        buttons: bool = True,
    ) -> None:
        """ส่งคำตอบ (ตัดเป็นหลายข้อความถ้ายาว) พร้อมปุ่มใต้ข้อความสุดท้าย

        existing: ข้อความ preview จาก streaming — จะถูกแก้เป็นก้อนแรกของคำตอบแทนการส่งใหม่
        """
        view = AnswerView(self, ctx) if buttons else None
        chunks, file = self._render(ctx)
        try:
            try:
                await self._send_chunks(ctx, send, existing, view, chunks, file)
            except discord.Forbidden as e:
                if file is None:
                    raise
                # ไม่มีสิทธิ์ Attach Files → ส่งคำตอบเต็มแบบแบ่งหลายข้อความแทน
                log.warning("แนบไฟล์คำตอบไม่ได้ (ขาดสิทธิ์ Attach Files?) ส่งแบบแบ่งข้อความแทน: %r", e)
                self.admin_log.post(
                    "📄 แนบไฟล์คำตอบยาวไม่ได้ (บอทขาดสิทธิ์ **Attach Files**) — ส่งแบบแบ่งหลายข้อความแทน",
                    key="attach_forbidden",
                )
                chunks, _ = self._render(ctx, allow_file=False)
                await self._send_chunks(ctx, send, existing, view, chunks, None)
        except discord.HTTPException as e:
            log.exception("Failed to send answer")
            if view is not None:
                view.stop()
            # อย่าให้ผู้ใช้รอเงียบ ๆ: แจ้งในห้อง + ส่งรายละเอียดเข้าห้อง log
            self.admin_log.post(
                f"❌ ส่งคำตอบเข้า Discord ไม่สำเร็จ: `{e!r}`"[:500],
                key=f"deliver:{type(e).__name__}:{e.status}",
            )
            try:
                await send("⚠️ AI ตอบแล้วแต่ส่งคำตอบไม่สำเร็จ ลองกด 🔄 หรือถามใหม่อีกครั้งนะ", None)
            except discord.HTTPException:
                pass

    async def _send_chunks(
        self,
        ctx: AnswerContext,
        send: Sender,
        existing: discord.Message | discord.WebhookMessage | None,
        view: AnswerView | None,
        chunks: list[str],
        file: discord.File | None,
    ) -> None:
        ctx.message_ids = []
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            attach = file if last else None
            if i == 0 and existing is not None:
                extra = {"attachments": [attach]} if attach else {}
                await existing.edit(content=chunk, view=view if last else None, **extra)
                msg = existing
            else:
                msg = await send(chunk, view if last else None, attach)
            ctx.message_ids.append(msg.id)
            if last and view is not None:
                view.message = msg

    def _channel_sender(
        self, channel: discord.abc.Messageable, reply_to: discord.Message | None = None
    ) -> Sender:
        first = True

        async def send(content: str, view: discord.ui.View | None, file: discord.File | None = None):
            nonlocal first
            kwargs: dict = {"view": view} if view else {}
            if file:
                kwargs["file"] = file
            if first and reply_to is not None:
                first = False
                return await reply_to.reply(content, allowed_mentions=REPLY_MENTIONS, **kwargs)
            first = False
            return await channel.send(content, **kwargs)

        return send

    @staticmethod
    def _followup_sender(interaction: discord.Interaction) -> Sender:
        async def send(content: str, view: discord.ui.View | None, file: discord.File | None = None):
            kwargs: dict = {"view": view} if view else {}
            if file:
                kwargs["file"] = file
            return await interaction.followup.send(content, wait=True, **kwargs)

        return send

    # ---------- ปุ่มใต้คำตอบ ----------

    async def _delete_messages(self, ctx: AnswerContext, ids: Sequence[int]) -> None:
        for mid in ids:
            try:
                await ctx.channel.get_partial_message(mid).delete()  # type: ignore[attr-defined]
            except discord.HTTPException:
                pass

    async def regenerate_answer(self, interaction: discord.Interaction, view: AnswerView) -> None:
        if wait_msg := self._limit_message(interaction.user):
            await interaction.response.send_message(wait_msg, ephemeral=True)
            return
        view.busy = True
        try:
            await self._regenerate(interaction, view)
        finally:
            view.busy = False

    async def _regenerate(self, interaction: discord.Interaction, view: AnswerView) -> None:
        ctx = view.ctx
        await interaction.response.defer()
        if ctx.ok:
            self.memory.remove_exchange(ctx.channel_id, ctx.prompt, ctx.answer)
        async with ctx.channel.typing():
            await self._run(ctx)
        view.refresh()

        # ข้อความที่มีปุ่ม (ข้อความสุดท้าย) จะถูกแก้เป็นคำตอบใหม่ ส่วนข้อความอื่นของคำตอบเดิมลบทิ้ง
        source_id = interaction.message.id if interaction.message else None
        await self._delete_messages(ctx, [m for m in ctx.message_ids if m != source_id])
        chunks, file = self._render(ctx)
        single = len(chunks) == 1
        # attachments=[] ลบไฟล์ของคำตอบเดิม (ถ้ามี) ออกด้วย
        await interaction.edit_original_response(
            content=chunks[0], view=view if single else None,
            attachments=[file] if file and single else [],
        )
        ctx.message_ids = [source_id] if source_id else []
        for i, chunk in enumerate(chunks[1:], start=1):
            last = i == len(chunks) - 1
            kwargs: dict = {"view": view} if last else {}
            if last and file:
                kwargs["file"] = file
            msg = await interaction.followup.send(chunk, wait=True, **kwargs)
            ctx.message_ids.append(msg.id)
            if last:
                view.message = msg

    async def followup_answer(
        self, interaction: discord.Interaction, view: AnswerView, kind: str
    ) -> None:
        """ปุ่ม ➡️ เขียนต่อ / 📝 สั้นลง / 📖 ละเอียดขึ้น / 🌐 แปล — ส่งเป็นคำตอบใหม่ต่อท้าย"""
        if wait_msg := self._limit_message(interaction.user):
            await interaction.response.send_message(wait_msg, ephemeral=True)
            return
        view.busy = True
        try:
            await self._followup(interaction, view, kind)
        finally:
            view.busy = False

    async def _followup(self, interaction: discord.Interaction, view: AnswerView, kind: str) -> None:
        old = view.ctx
        await interaction.response.defer()
        if kind == "continue":
            # ปุ่ม "เขียนต่อ" ของคำตอบเดิมไม่ต้องใช้แล้ว
            view.remove_item(view.continue_)
            try:
                await interaction.edit_original_response(view=view)
            except discord.HTTPException:
                pass
            question, header, use_memory = CONTINUE_QUESTION, "", True
        else:
            label, instruction = FOLLOWUPS[kind]
            question = f"{instruction}\n\n{old.answer[:6000]}"
            header, use_memory = f"-# {label}\n", False
        ctx = AnswerContext(
            channel=old.channel, channel_id=old.channel_id, asker_id=old.asker_id,
            asker_name=old.asker_name, question=question, images=(), header=header,
            guild_id=old.guild_id, exempt=old.exempt, use_memory=use_memory,
        )
        async with ctx.channel.typing():
            existing = await self._run_streaming(
                ctx, lambda content: interaction.followup.send(content, wait=True)
            )
        await self._deliver(ctx, self._followup_sender(interaction), existing)

    async def delete_answer(self, interaction: discord.Interaction, view: AnswerView) -> None:
        ctx = view.ctx
        await interaction.response.defer()
        if ctx.ok:
            self.memory.remove_exchange(ctx.channel_id, ctx.prompt, ctx.answer)
        view.stop()
        await self._delete_messages(ctx, ctx.message_ids)

    @staticmethod
    def _is_exempt(user: discord.abc.User) -> bool:
        """แอดมิน (Administrator / Manage Server) ไม่ถูกจำกัดโควต้ารายวัน"""
        if not isinstance(user, discord.Member):
            return False
        perms = user.guild_permissions
        return perms.administrator or perms.manage_guild

    def _limit_message(self, user: discord.abc.User) -> str | None:
        """เช็กโควต้ารายวันก่อน แล้วค่อยเช็ก cooldown คืนข้อความแจ้งผู้ใช้ถ้าถามไม่ได้"""
        limit = self.config.daily_limit
        if limit and not self._is_exempt(user):
            if self.db.used_today(self._today(), user.id) >= limit:
                self.admin_log.post(
                    f"📊 **{user.display_name}** ใช้ครบโควต้า {limit} คำถามของวันนี้แล้ว",
                    key=f"quota:{self._today()}:{user.id}", cooldown=86400,
                )
                return f"📊 วันนี้คุณถามครบ {limit} คำถามแล้ว โควต้าจะรีเซ็ตตอนเที่ยงคืน แล้วเจอกันพรุ่งนี้นะ 🙏"
        return self._cooldown_message(user.id)

    def _cooldown_message(self, user_id: int) -> str | None:
        remaining = self.cooldown.check(user_id)
        if remaining > 0:
            return f"🕒 ใจเย็น ๆ นะ รออีก {remaining:.0f} วินาทีแล้วค่อยถามใหม่"
        return None

    # ---------- mention / ห้องคุยกับ AI ----------

    def _in_ai_channel(self, channel: discord.abc.Messageable) -> bool:
        if self.ai_channels.is_ai_channel(getattr(channel, "id", 0)):
            return True
        # เธรดที่แตกออกจากห้องคุยกับ AI ก็ตอบอัตโนมัติด้วย (ความจำแยกตามเธรด)
        return isinstance(channel, discord.Thread) and self.ai_channels.is_ai_channel(
            channel.parent_id
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        # ข้ามข้อความระบบ เช่น ปักหมุด, มีคนเข้าเซิร์ฟเวอร์
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        in_ai_channel = self._in_ai_channel(message.channel)
        mentioned = self.user in message.mentions
        if not (in_ai_channel or mentioned):
            return
        if message.content.lstrip().startswith(IGNORE_PREFIX):
            return

        # ตัด mention ของบอทออกจากข้อความ
        question = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        if match := REMEMBER_PREFIX.match(question):
            await message.reply(self.remember(message.author.id, match.group(2)))
            return
        # ถ้าข้อความนี้ reply ข้อความอื่น ให้ AI เห็นข้อความ/รูปนั้นด้วย
        quote, quoted_attachments = await self._replied_context(message)
        attachments = [*message.attachments, *quoted_attachments]
        has_image = self.config.max_images > 0 and any(map(is_image, attachments))
        has_file = self.config.max_file_chars > 0 and any(map(is_document, attachments))
        if not question and not has_image and not has_file and not quote:
            # ในห้องคุยกับ AI ข้อความที่มีแต่สติกเกอร์/ไฟล์อื่น ให้ข้ามไปเงียบ ๆ
            if not in_ai_channel:
                await message.reply("สวัสดี! พิมพ์คำถามต่อจากการ mention ได้เลย หรือใช้ `/ask` ก็ได้ 😊")
            return

        if wait_msg := self._limit_message(message.author):
            await message.reply(wait_msg, delete_after=10)
            return

        images: list[ImageData] = []
        file_text, file_names = "", ()
        if has_image or has_file:
            images, file_text, file_names, skipped = await self._read_attachments(attachments)
            if skipped:
                await message.reply(
                    "📎 ข้ามไฟล์เหล่านี้: " + ", ".join(skipped), delete_after=20,
                    allowed_mentions=REPLY_MENTIONS,
                )
            if not images and not file_names and not question and not quote:
                return

        typed = question
        if not question:
            if file_names:
                question = FILE_ONLY_QUESTION
            elif images and not quote:
                question = IMAGE_ONLY_QUESTION
            else:
                question = REPLY_ONLY_QUESTION
        ctx = AnswerContext(
            channel=message.channel, channel_id=message.channel.id,
            asker_id=message.author.id, asker_name=message.author.display_name,
            question=f"{quote}\n{question}" if quote else question,
            images=tuple(images), header="",
            guild_id=message.guild.id if message.guild else None,
            exempt=self._is_exempt(message.author), search_query=typed,
            attachments_text=file_text, file_names=file_names,
        )
        await self._react(message, REACT_THINKING)
        # โหมดเธรด: คำถามใหม่ในห้อง AI เปิดเธรดของตัวเอง แล้วตอบในเธรด (ความจำแยกตามเธรด)
        target: discord.abc.Messageable = message.channel
        reply_to: discord.Message | None = message
        new_thread: discord.Thread | None = None
        if in_ai_channel and self._thread_mode(message.channel):
            new_thread = await self._open_thread(message, typed)
            if new_thread is not None:
                target, reply_to = new_thread, None
                ctx.channel, ctx.channel_id = new_thread, new_thread.id

        def start(content: str):
            if reply_to is None:
                return target.send(content)
            return reply_to.reply(content, allowed_mentions=REPLY_MENTIONS)

        # แสดง "กำลังพิมพ์..." ระหว่างรอ AI แล้วค่อย ๆ แสดงคำตอบเมื่อเริ่มได้ข้อความ
        async with target.typing():
            existing = await self._run_streaming(ctx, start)
        await self._unreact(message, REACT_THINKING)
        if not ctx.ok:
            await self._react(message, REACT_ERROR)
        # มี preview แล้ว = ส่งข้อความแรกไปแล้ว ข้อความที่เหลือส่งต่อท้ายธรรมดา
        sender = self._channel_sender(target, reply_to=None if existing else reply_to)
        await self._deliver(ctx, sender, existing)
        if new_thread is not None and ctx.ok and self.config.thread_auto_title:
            # ตั้งชื่อเธรดให้ตรงเรื่องเบื้องหลัง ไม่ต้องรอ
            task = asyncio.get_running_loop().create_task(self._auto_title(new_thread, ctx))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _auto_title(self, thread: discord.Thread, ctx: AnswerContext) -> None:
        """ให้ AI ตั้งชื่อเธรดสั้น ๆ ตามคำถาม + คำตอบแรก (ไม่นับโควต้าผู้ใช้)"""
        prompt = (
            "ตั้งชื่อหัวข้อสั้น ๆ ไม่เกิน 6 คำ ให้กับบทสนทนาด้านล่าง ใช้ภาษาเดียวกับคำถาม "
            "ตอบเฉพาะชื่อหัวข้อบรรทัดเดียว ไม่ใส่เครื่องหมายคำพูด อีโมจิ หรือคำอธิบาย\n\n"
            f"คำถาม: {(ctx.search_query or ctx.question)[:500]}\nคำตอบ: {ctx.answer[:800]}"
        )
        try:
            result = await self.ai.generate([], prompt)
        except AIError as e:
            log.info("ตั้งชื่อเธรดไม่สำเร็จ: %r", e)
            return
        title = result.text.strip().splitlines()[0].strip(" \"'“”‘’*#`.:")[:80] if result.text.strip() else ""
        if not title:
            return
        try:
            await thread.edit(name=f"💬 {title}")
        except discord.HTTPException as e:
            log.info("เปลี่ยนชื่อเธรดไม่ได้: %r", e)

    def _thread_mode(self, channel: discord.abc.Messageable) -> bool:
        if isinstance(channel, discord.Thread) or not isinstance(channel, discord.TextChannel):
            return False
        return self.db.get_setting(f"thread_mode:{channel.id}") == "1"

    async def _open_thread(self, message: discord.Message, typed: str) -> discord.Thread | None:
        """เปิดเธรดจากข้อความคำถาม (ต้องมีสิทธิ์ Create Public Threads) — ไม่ได้ก็ตอบในห้องปกติ"""
        title = " ".join(typed.split())[:80] or "📎 คำถามพร้อมไฟล์แนบ"
        try:
            thread = await message.create_thread(name=f"💬 {title}", auto_archive_duration=60)
        except discord.HTTPException as e:
            log.warning("เปิดเธรดไม่ได้ (ขาดสิทธิ์ Create Public Threads?): %r", e)
            return None
        try:
            await thread.send(
                "-# 🧵 คุยต่อในเธรดนี้ได้เลย บอทจำบทสนทนาแยกเฉพาะเธรดนี้ · กด 🔒 เมื่อคุยจบ",
                view=CloseThreadView(),
            )
        except discord.HTTPException:
            pass
        return thread

    async def _read_attachments(
        self, attachments: list[discord.Attachment]
    ) -> tuple[list[ImageData], str, tuple[str, ...], list[str]]:
        """อ่านรูป + ไฟล์เอกสารที่แนบมา คืน (รูป/PDF สแกน, เนื้อหาไฟล์, ชื่อไฟล์, เหตุผลที่ข้าม)"""
        images: list[ImageData] = []
        skipped: list[str] = []
        if self.config.max_images and any(map(is_image, attachments)):
            images, skipped = await read_images(
                attachments, self.config.max_images, self.config.max_image_bytes
            )
        text, names = "", ()
        if self.config.max_file_chars and any(map(is_document, attachments)):
            text, pdfs, name_list, doc_skipped = await read_documents(
                attachments, self.config.max_file_bytes, self.config.max_file_chars
            )
            images += pdfs
            names = tuple(name_list)
            skipped += doc_skipped
        return images, text, names, skipped

    async def _replied_context(
        self, message: discord.Message
    ) -> tuple[str, list[discord.Attachment]]:
        """ข้อความ + รูปของข้อความที่ถูก reply (ข้ามถ้าเป็นข้อความของบอทเอง เพราะอยู่ในความจำแล้ว)"""
        ref = message.reference
        if ref is None or ref.message_id is None:
            return "", []
        replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if replied is None:
            try:
                replied = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return "", []
        if self.user is not None and replied.author.id == self.user.id:
            return "", []

        text, images = self._message_content(replied, limit=1500)
        if not text and not images:
            return "", []
        who = replied.author.display_name
        quote = (
            f'[ข้อความที่ถูกตอบกลับ จาก {who}]: "{text}"' if text
            else f"[รูปที่ถูกตอบกลับ จาก {who}]"
        )
        return quote, images

    @staticmethod
    def _message_content(
        message: discord.Message, limit: int = 4000
    ) -> tuple[str, list[discord.Attachment]]:
        """ข้อความ (รวมข้อความใน embed ของบอทอื่น) + ไฟล์รูปของข้อความหนึ่ง"""
        parts = [message.content.strip()]
        for embed in message.embeds[:2]:
            parts += [embed.title or "", embed.description or ""]
            parts += [f"{f.name}: {f.value}" for f in embed.fields[:10]]
        text = "\n".join(p for p in parts if p)[:limit]
        return text, [a for a in message.attachments if is_image(a) or is_document(a)]

    async def message_action(
        self, interaction: discord.Interaction, message: discord.Message, action: str
    ) -> None:
        """เมนูคลิกขวาที่ข้อความ → Apps → แปล / สรุป / อธิบาย (ผลลัพธ์เห็นคนเดียว)"""
        label, instruction = MESSAGE_ACTIONS[action]
        if wait_msg := self._limit_message(interaction.user):
            await interaction.response.send_message(wait_msg, ephemeral=True)
            return
        text, attachments = self._message_content(message)
        if not text and not attachments:
            await interaction.response.send_message(
                "ข้อความนี้ไม่มีตัวหนังสือหรือรูปให้ AI อ่านนะ", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        images, file_text, file_names, _ = await self._read_attachments(attachments)
        who = message.author.display_name
        question = f"{instruction}\n\n[ข้อความจาก {who}]:\n{text or '(มีแต่ไฟล์แนบ)'}"
        ctx = AnswerContext(
            channel=interaction.channel,  # type: ignore[arg-type]
            channel_id=interaction.channel_id or interaction.user.id,
            asker_id=interaction.user.id, asker_name=interaction.user.display_name,
            question=question, images=tuple(images),
            header=f"-# {label} ข้อความของ {who} · {message.jump_url}\n",
            guild_id=interaction.guild_id, exempt=self._is_exempt(interaction.user),
            attachments_text=file_text, file_names=file_names, use_memory=False,
        )

        async def send(
            content: str, view: discord.ui.View | None = None, file: discord.File | None = None
        ):
            extra = {"file": file} if file else {}
            return await interaction.followup.send(content, ephemeral=True, wait=True, **extra)

        existing = await self._run_streaming(ctx, lambda content: send(content))
        await self._deliver(ctx, send, existing, buttons=False)

    async def _react(self, message: discord.Message, emoji: str) -> None:
        # ต้องมีสิทธิ์ Add Reactions + Read Message History ถ้าไม่มีก็ข้ามไปเงียบ ๆ
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    async def _unreact(self, message: discord.Message, emoji: str) -> None:
        if self.user is None:
            return
        try:
            await message.remove_reaction(emoji, self.user)
        except discord.HTTPException:
            pass

    # ---------- slash commands ----------

    def _register_commands(self) -> None:
        @self.tree.command(name="ask", description="ถามคำถามกับ AI")
        @app_commands.describe(question="คำถามของคุณ", file="(ไม่บังคับ) แนบรูป / PDF / ไฟล์ข้อความหรือโค้ด")
        async def ask(
            interaction: discord.Interaction,
            question: str,
            file: discord.Attachment | None = None,
        ) -> None:
            if wait_msg := self._limit_message(interaction.user):
                await interaction.response.send_message(wait_msg, ephemeral=True)
                return

            # defer: บอก Discord ว่ากำลังประมวลผล (ขึ้น "กำลังคิด...") และขยายเวลาตอบจาก 3 วินาทีเป็น 15 นาที
            await interaction.response.defer(thinking=True)
            images: list[ImageData] = []
            file_text, file_names, note = "", (), ""
            if file is not None:
                images, file_text, file_names, skipped = await self._read_attachments([file])
                if skipped:
                    note = "\n-# 📎 ข้ามไฟล์: " + ", ".join(skipped)
                elif not images and not file_names:
                    note = "\n-# 📎 ไฟล์ชนิดนี้ยังอ่านไม่ได้ (รองรับรูป, PDF, ไฟล์ข้อความ/โค้ด)"

            # แสดงคำถามด้วย เพราะคนอื่นในช่องจะไม่เห็นว่าถามอะไร
            attached = f" 📎 {', '.join(file_names)}" if file_names else (" 📷" if images else "")
            header = f"> **{interaction.user.display_name}:** {question[:300]}{attached}{note}\n\n"
            ctx = AnswerContext(
                channel=interaction.channel,  # type: ignore[arg-type]
                channel_id=interaction.channel_id or interaction.user.id,
                asker_id=interaction.user.id, asker_name=interaction.user.display_name,
                question=question, images=tuple(images), header=header,
                guild_id=interaction.guild_id, exempt=self._is_exempt(interaction.user),
                search_query=question, attachments_text=file_text, file_names=file_names,
            )
            existing = await self._run_streaming(
                ctx, lambda content: interaction.followup.send(content, wait=True)
            )
            await self._deliver(ctx, self._followup_sender(interaction), existing)

        @self.tree.command(name="reset", description="ล้างความจำบทสนทนาของ AI ในช่องนี้")
        async def reset(interaction: discord.Interaction) -> None:
            channel_id = interaction.channel_id or interaction.user.id
            self.memory.reset(channel_id)
            await interaction.response.send_message("🧹 ล้างความจำของช่องนี้แล้ว เริ่มคุยใหม่ได้เลย!")

        @self.tree.command(
            name="aichannel", description="ตั้งห้องนี้เป็นห้องคุยกับ AI (บอทตอบทุกข้อความ ไม่ต้อง /ask)"
        )
        @app_commands.describe(mode="เปิด / ปิด / ดูสถานะ")
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="เปิด — บอทตอบทุกข้อความในห้องนี้", value="on"),
                app_commands.Choice(name="เปิดแบบเธรด — คำถามใหม่เปิดเธรดของตัวเอง ห้องไม่รก", value="thread"),
                app_commands.Choice(name="ปิด — กลับไปใช้ /ask หรือ mention", value="off"),
                app_commands.Choice(name="สถานะ — ดูว่าห้องไหนเปิดอยู่", value="status"),
            ]
        )
        @app_commands.guild_only()
        # ค่าเริ่มต้น: เฉพาะคนที่มีสิทธิ์ Manage Channels เห็นคำสั่งนี้
        # (แอดมินปรับได้ที่ Server Settings → Integrations)
        @app_commands.default_permissions(manage_channels=True)
        async def aichannel(
            interaction: discord.Interaction, mode: app_commands.Choice[str]
        ) -> None:
            channel_id = interaction.channel_id
            if channel_id is None:
                await interaction.response.send_message("ใช้คำสั่งนี้ในห้องของเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
                return

            if mode.value in ("on", "thread"):
                self.ai_channels.enable(channel_id)
                if mode.value == "thread":
                    self.db.set_setting(f"thread_mode:{channel_id}", "1")
                else:
                    self.db.delete_setting(f"thread_mode:{channel_id}")
                await interaction.response.send_message(
                    embed=self._welcome_embed(thread_mode=mode.value == "thread")
                )
                # ปักหมุดการ์ดต้อนรับไว้ (ต้องมีสิทธิ์ Pin/Manage Messages ถ้าไม่มีก็ข้าม)
                try:
                    welcome = await interaction.original_response()
                    await welcome.pin(reason="การ์ดวิธีใช้ห้องคุยกับ AI")
                except discord.HTTPException:
                    log.info("Could not pin welcome message in %s (missing permission?)", channel_id)
            elif mode.value == "off":
                if self.ai_channels.is_fixed(channel_id):
                    await interaction.response.send_message(
                        "⚠️ ห้องนี้ถูกตั้งไว้ใน `AI_CHANNEL_IDS` ของไฟล์ .env "
                        "ต้องลบ ID ออกจากไฟล์นั้นแล้วรีสตาร์ทบอท",
                        ephemeral=True,
                    )
                    return
                self.ai_channels.disable(channel_id)
                self.db.delete_setting(f"thread_mode:{channel_id}")
                await interaction.response.send_message(
                    "⏹️ ปิดห้องคุยกับ AI แล้ว ห้องนี้กลับไปใช้ `/ask` หรือ mention บอทเหมือนเดิม"
                )
            else:
                guild = interaction.guild
                mine = sorted(
                    cid for cid in self.ai_channels.all_ids()
                    if guild is None or guild.get_channel_or_thread(cid) is not None
                )
                listing = "\n".join(f"• <#{cid}>" for cid in mine) or "ยังไม่มี"
                here = "✅ เปิดอยู่" if self._in_ai_channel(interaction.channel) else "❌ ปิดอยู่"
                await interaction.response.send_message(
                    f"ห้องนี้: {here}\n**ห้องคุยกับ AI ในเซิร์ฟเวอร์นี้:**\n{listing}",
                    ephemeral=True,
                )

        # เมนูคลิกขวาที่ข้อความ → Apps (Discord จำกัดชื่อไม่เกิน 32 ตัวอักษร และสูงสุด 5 เมนู)
        @self.tree.context_menu(name="แปลภาษา (AI)")
        async def translate_menu(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.message_action(interaction, message, "translate")

        @self.tree.context_menu(name="สรุปข้อความ (AI)")
        async def summarize_menu(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.message_action(interaction, message, "summarize")

        @self.tree.context_menu(name="อธิบายข้อความ (AI)")
        async def explain_menu(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.message_action(interaction, message, "explain")

        @self.tree.command(name="logchannel", description="ตั้งห้องนี้เป็นห้อง log ของแอดมิน (แจ้งปัญหาของบอท + สรุปรายวัน)")
        @app_commands.describe(mode="ตั้ง / ปิด / ทดสอบ")
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="ตั้งห้องนี้เป็นห้อง log", value="set"),
                app_commands.Choice(name="ปิดห้อง log", value="off"),
                app_commands.Choice(name="ทดสอบส่งข้อความ + สรุปสถิติวันนี้", value="test"),
            ]
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        async def logchannel(
            interaction: discord.Interaction, mode: app_commands.Choice[str]
        ) -> None:
            if mode.value == "set":
                self.admin_log.set_channel(interaction.channel_id)
                await interaction.response.send_message(
                    "📋 ตั้งห้องนี้เป็น **ห้อง log** แล้ว บอทจะแจ้งที่นี่เมื่อ:\n"
                    "• สลับไปใช้ AI สำรอง / AI ใช้ไม่ได้ทุกตัว\n"
                    "• โมเดลใน .env ถูกถอด (ตอนเริ่มบอท) · ค้นเว็บไม่สำเร็จ · มีคนใช้ครบโควต้า\n"
                    "• 🌙 สรุปสถิติทุกเที่ยงคืน"
                )
            elif mode.value == "off":
                self.admin_log.set_channel(None)
                await interaction.response.send_message("⏹️ ปิดห้อง log แล้ว", ephemeral=True)
            else:
                if self.admin_log.channel_id is None:
                    await interaction.response.send_message(
                        "ยังไม่ได้ตั้งห้อง log — ใช้ `/logchannel ตั้งห้องนี้เป็นห้อง log` ก่อน", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    f"ส่งข้อความทดสอบไปที่ <#{self.admin_log.channel_id}> แล้ว", ephemeral=True
                )
                self.admin_log.post("🧪 ทดสอบห้อง log: ใช้งานได้ ✅")
                self.admin_log.post(embed=self._stats_embed(interaction.guild_id))

        persona_choices = [
            app_commands.Choice(name="ดูบุคลิกปัจจุบันของห้องนี้", value="view"),
            app_commands.Choice(name="ค่าเริ่มต้น (ตาม SYSTEM_PROMPT)", value="default"),
            *[app_commands.Choice(name=label, value=key) for key, (label, _) in persona.PRESETS.items()],
            app_commands.Choice(name="✏️ กำหนดเอง (พิมพ์ในช่อง text)", value="custom"),
        ]

        @self.tree.command(name="persona", description="ตั้งบุคลิกของบอทในห้องนี้")
        @app_commands.describe(mode="เลือกบุคลิก", text="บุคลิกที่กำหนดเอง (ใช้กับ ✏️ กำหนดเอง)")
        @app_commands.choices(mode=persona_choices)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_channels=True)
        async def persona_cmd(
            interaction: discord.Interaction,
            mode: app_commands.Choice[str],
            text: str | None = None,
        ) -> None:
            channel_id = interaction.channel_id or 0
            key = persona.key_for(channel_id)
            if mode.value == "view":
                saved = self.db.get_setting(key) or ""
                current = persona.resolve(saved)
                if current is None:
                    msg = "🎭 ห้องนี้ใช้บุคลิก **ค่าเริ่มต้น** (ตาม SYSTEM_PROMPT)"
                else:
                    msg = f"🎭 บุคลิกของห้องนี้: **{current[0]}**"
                    if saved.startswith("custom:"):
                        msg += f"\n> {saved.removeprefix('custom:')[:300]}"
                await interaction.response.send_message(msg, ephemeral=True)
                return
            if mode.value == "default":
                self.db.delete_setting(key)
                label = "ค่าเริ่มต้น"
            elif mode.value == "custom":
                if not text or not text.strip():
                    await interaction.response.send_message(
                        "พิมพ์บุคลิกที่ต้องการในช่อง `text` ด้วยนะ เช่น "
                        "`พูดเหมือนโจรสลัด ชอบเล่าเรื่องผจญภัย`",
                        ephemeral=True,
                    )
                    return
                self.db.set_setting(key, "custom:" + text.strip()[: persona.CUSTOM_MAX])
                label = "✏️ กำหนดเอง"
            else:
                self.db.set_setting(key, mode.value)
                label = persona.PRESETS[mode.value][0]
            await interaction.response.send_message(
                f"🎭 เปลี่ยนบุคลิกของบอทในห้องนี้เป็น **{label}** แล้ว\n"
                "-# ถ้าบอทยังพูดสไตล์เดิม ให้ใช้ `/reset` ล้างความจำของห้องก่อน"
            )

        @self.tree.command(name="remember", description="ให้บอทจำข้อมูลของคุณไว้ (ใช้ได้ทุกห้อง)")
        @app_commands.describe(note="สิ่งที่อยากให้จำ เช่น ฉันชื่อปีเตอร์ ชอบเล่น FiveM")
        async def remember_cmd(interaction: discord.Interaction, note: str) -> None:
            await interaction.response.send_message(
                self.remember(interaction.user.id, note), ephemeral=True
            )

        @self.tree.command(name="memory", description="ดูข้อมูลที่บอทจำเกี่ยวกับคุณไว้")
        async def memory_cmd(interaction: discord.Interaction) -> None:
            notes = self.db.notes(interaction.user.id)
            if not notes:
                text = "ยังไม่ได้ให้บอทจำอะไรเลย ลองพิมพ์ `/remember` หรือ `จำไว้ว่า ...` ในห้อง AI"
            else:
                listing = "\n".join(f"`{i}.` {n}" for i, n in enumerate(notes, 1))
                text = f"📝 **สิ่งที่บอทจำเกี่ยวกับคุณ** ({len(notes)}/{MAX_NOTES})\n{listing}\n-# ลบด้วย `/forget`"
            await interaction.response.send_message(text, ephemeral=True)

        @self.tree.command(name="forget", description="ลบข้อมูลที่บอทจำเกี่ยวกับคุณ")
        @app_commands.describe(number="ลำดับที่จะลบ (ดูจาก /memory) — เว้นว่าง = ลบทั้งหมด")
        async def forget_cmd(interaction: discord.Interaction, number: int | None = None) -> None:
            if number is None:
                count = self.db.clear_notes(interaction.user.id)
                text = f"🧹 ลบข้อมูลที่จำไว้ทั้งหมดแล้ว ({count} ข้อ)"
            elif (removed := self.db.delete_note(interaction.user.id, number)) is not None:
                text = f"🗑️ ลบข้อ {number} แล้ว: ~~{removed}~~"
            else:
                text = f"ไม่มีข้อที่ {number} — ดูลำดับได้ด้วย `/memory`"
            await interaction.response.send_message(text, ephemeral=True)

        @self.tree.command(name="usage", description="ดูว่าวันนี้ถาม AI ไปแล้วกี่ครั้ง")
        async def usage(interaction: discord.Interaction) -> None:
            used = self.db.used_today(self._today(), interaction.user.id)
            limit = self.config.daily_limit
            if not limit or self._is_exempt(interaction.user):
                text = f"📊 วันนี้คุณถามไปแล้ว **{used}** ครั้ง (ไม่จำกัดจำนวน)"
            else:
                text = (
                    f"📊 วันนี้คุณถามไปแล้ว **{used}/{limit}** ครั้ง "
                    f"เหลืออีก **{max(0, limit - used)}** ครั้ง (รีเซ็ตตอนเที่ยงคืน)"
                )
            await interaction.response.send_message(text, ephemeral=True)

        @self.tree.command(name="stats", description="สถิติการใช้งานบอท AI (สำหรับแอดมิน)")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        async def stats(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                embed=self._stats_embed(interaction.guild_id), ephemeral=True
            )

        @self.tree.command(name="help", description="วิธีใช้บอท AI")
        async def help_(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=self._welcome_embed(), ephemeral=True)

        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            log.exception("Slash command error", exc_info=error)
            msg = "⚠️ เกิดข้อผิดพลาดในการประมวลผลคำสั่ง ลองใหม่อีกครั้งนะ"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass


    def _stats_embed(self, guild_id: int | None, day: str | None = None) -> discord.Embed:
        today = day or self._today()
        s = self.db.day_stats(today, guild_id)
        title = "📈 สถิติบอท AI วันนี้" if day is None else f"🌙 สรุปสถิติบอท AI ประจำวันที่ {today}"
        embed = discord.Embed(title=title, color=BRAND_COLOR)
        embed.add_field(name="💬 คำตอบ", value=f"**{s.answers:,}**", inline=True)
        embed.add_field(name="👥 ผู้ใช้", value=f"**{s.users:,}** คน", inline=True)
        embed.add_field(name="⚡ เวลาเฉลี่ย", value=f"**{s.avg_elapsed:.1f}** วิ", inline=True)
        embed.add_field(name="⚠️ error", value=f"{s.errors:,}", inline=True)
        embed.add_field(name="🛟 ใช้ตัวสำรอง", value=f"{s.backup:,}", inline=True)
        embed.add_field(name="🔎 ค้นเว็บ", value=f"{s.searched:,}", inline=True)

        top = self.db.top_users(today, guild_id)
        medals = ["🥇", "🥈", "🥉", "4.", "5."]
        embed.add_field(
            name="🏆 ถามมากที่สุด",
            value="\n".join(f"{medals[i]} {name} — {n}" for i, (name, n) in enumerate(top))
            or "ยังไม่มี",
            inline=False,
        )
        models = self.db.models_used(today, guild_id)
        embed.add_field(
            name="🤖 โมเดลที่ตอบ",
            value="\n".join(f"`{m}` — {n}" for m, n in models) or "ยังไม่มี",
            inline=False,
        )
        # กราฟแท่งเล็ก ๆ ของ 7 วันล่าสุด
        end = datetime.strptime(today, "%Y-%m-%d")
        days = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        totals = self.db.daily_totals(days, guild_id)
        peak = max((n for _, n in totals), default=0) or 1
        bars = "▁▂▃▄▅▆▇█"
        chart = "".join(bars[min(7, round(n / peak * 7))] for _, n in totals)
        week = sum(n for _, n in totals)
        embed.add_field(
            name="📅 7 วันล่าสุด", value=f"`{chart}`  รวม **{week:,}** คำตอบ", inline=False
        )
        limit = self.config.daily_limit
        quota = f"{limit} คำถาม/คน/วัน" if limit else "ไม่จำกัด"
        backup = self.config.backup_provider or "ไม่มี"
        search = "Tavily" if self.config.tavily_api_key else ("Gemini" if self.config.web_search else "ปิด")
        embed.set_footer(
            text=f"AI: {self.config.provider} · สำรอง: {backup} · ค้นเว็บ: {search} · โควต้า: {quota}"
        )
        return embed

    def _welcome_embed(self, thread_mode: bool = False) -> discord.Embed:
        embed = discord.Embed(
            title="💬 ห้องคุยกับ AI",
            description=(
                "พิมพ์คุยได้เลย **ไม่ต้องใช้ `/ask` หรือ mention** บอทจะตอบทุกข้อความในห้องนี้\n"
                "ห้องอื่นใช้ `/ask` หรือ `@บอท คำถาม` ได้เหมือนเดิม"
            ),
            color=BRAND_COLOR,
        )
        if thread_mode:
            embed.add_field(
                name="🧵 โหมดเธรด",
                value="พิมพ์คำถามในห้องนี้ บอทจะเปิดเธรดใหม่ให้ แล้วคุยต่อในเธรดนั้นได้เลย (แต่ละเธรดจำแยกกัน)",
                inline=False,
            )
        if self.config.max_images:
            embed.add_field(
                name="📷 ส่งรูปได้",
                value=f"แนบรูปพร้อมคำถาม (png / jpg / webp สูงสุด {self.config.max_images} รูป)",
                inline=False,
            )
        embed.add_field(
            name="🔘 ปุ่มใต้คำตอบ",
            value="🔄 ตอบใหม่ · ➡️ เขียนต่อ · 🗑️ ลบ · 📝 สั้นลง · 📖 ละเอียดขึ้น · 🌐 แปล\n-# ใช้ได้ 10 นาที เฉพาะคนถาม",
            inline=False,
        )
        embed.add_field(
            name="📝 ให้บอทจำเรื่องของคุณ",
            value="พิมพ์ `จำไว้ว่า ฉันชื่อ... ชอบ...` หรือ `/remember` · ดู `/memory` · ลบ `/forget`",
            inline=False,
        )
        embed.add_field(
            name="🤫 คุยกันเอง",
            value=f"ขึ้นต้นข้อความด้วย `{IGNORE_PREFIX}` บอทจะไม่ตอบ",
            inline=False,
        )
        embed.add_field(name="🧹 ล้างความจำ", value="`/reset`", inline=True)
        embed.add_field(name="⏹️ ปิดห้องนี้", value="`/aichannel ปิด`", inline=True)
        if self.user:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        embed.set_footer(text=f"AI: {self.config.provider} · {self.config.model}")
        return embed


def main() -> None:
    try:
        config = Config.load()
    except ConfigError as e:
        log.error("ตั้งค่าไม่ถูกต้อง: %s (ดูตัวอย่างใน .env.example)", e)
        sys.exit(1)
    for warning in config.warnings:
        log.warning("การตั้งค่า: %s", warning)

    bot = AIChatBot(config)
    try:
        # log_handler=None เพราะเราตั้งค่า logging เองแล้วด้านบน
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error("DISCORD_TOKEN ไม่ถูกต้อง ตรวจสอบใน .env อีกครั้ง")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.error("ยังไม่ได้เปิด Message Content Intent ใน Discord Developer Portal (หน้า Bot)")
        sys.exit(1)


if __name__ == "__main__":
    main()
