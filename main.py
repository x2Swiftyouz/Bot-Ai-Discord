"""Discord AI Chatbot — จุดเริ่มต้นโปรแกรม

รัน: python main.py
"""

from __future__ import annotations

import itertools
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from bot.channels import AIChannelStore
from bot.config import Config, ConfigError
from bot.cooldown import UserCooldown
from bot.media import is_image, read_images
from bot.memory import ChannelMemory
from bot.providers import AIError, AIProvider, BackupProvider, ImageData, create_provider
from bot.search import SearchError, TavilySearch, format_results, should_search
from bot.storage import Database
from bot.utils import now_text, redact, split_message
from bot.views import AnswerContext, AnswerView

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
REPLY_ONLY_QUESTION = "ช่วยอธิบายหรือตอบข้อความนี้หน่อย"
MAX_SOURCES = 3
CONTINUE_QUESTION = "เขียนต่อจากคำตอบก่อนหน้าให้จบ ต่อจากจุดที่ค้างไว้เลย ไม่ต้องทวนซ้ำ"
REACT_THINKING = "👀"
REACT_ERROR = "⚠️"
BRAND_COLOR = discord.Color.from_rgb(88, 101, 242)

# ส่งข้อความ 1 ก้อน (พร้อมปุ่มถ้ามี) แล้วคืนข้อความที่ส่งไป
Sender = Callable[[str, "discord.ui.View | None"], Awaitable["discord.Message | discord.WebhookMessage"]]


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
        self.cooldown = UserCooldown(config.user_cooldown)
        self.ai_channels = AIChannelStore(
            config.ai_channel_ids, config.data_dir / "ai_channels.json"
        )
        self.ai: AIProvider | BackupProvider = create_provider(config)
        self.answer_count = 0
        self._cleaned_commands = False
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
        self.rotate_status.start()

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (ID %s) | provider=%s model=%s backup=%s",
            self.user, self.user.id if self.user else "?", self.config.provider, self.config.model,
            f"{self.config.backup_provider}/{self.config.backup_model}"
            if self.config.backup_provider else "-",
        )
        if not self._cleaned_commands:
            self._cleaned_commands = True
            await self._remove_stale_commands()

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

    async def ask_ai(self, ctx: AnswerContext) -> None:
        """ส่งคำถามไปยัง AI พร้อมบริบทของช่อง (+ ผลค้นเว็บถ้าต้องใช้) แล้วเก็บผลลัพธ์ลง ctx"""
        # ใส่ชื่อผู้ถาม เพราะในช่องเดียวอาจมีหลายคนคุยกับบอท
        prompt = f"{ctx.asker_name}: {ctx.question}"
        # ความจำเก็บแค่คำถาม ไม่เก็บรูปหรือผลค้นเว็บ (ประหยัดโควต้า) จึงจดไว้ว่ามีรูปแนบ
        ctx.prompt = prompt + (f" [แนบรูป {len(ctx.images)} รูป]" if ctx.images else "")
        ctx.footer = ""
        async with self.memory.lock(ctx.channel_id):
            history = self.memory.get(ctx.channel_id)
            started = time.monotonic()
            sources: tuple[tuple[str, str], ...] = ()
            if self.search and ctx.search_query and should_search(ctx.search_query):
                try:
                    found = await self.search.search(ctx.search_query)
                except SearchError as e:
                    log.warning("ค้นเว็บไม่สำเร็จ ตอบแบบไม่ค้นเว็บแทน: %s", e)
                    found = []
                if found:
                    prompt += "\n\n" + format_results(found, now_text(self.config.timezone))
                    sources = tuple((r.title, r.url) for r in found)
            try:
                result = await self.ai.generate(history, prompt, ctx.images)
            except AIError as e:
                log.warning("AI error in channel %s: %r", ctx.channel_id, e)
                ctx.ok, ctx.answer = False, e.user_message
            except Exception:
                log.exception("Unexpected error while calling AI")
                ctx.ok, ctx.answer = False, "⚠️ เกิดข้อผิดพลาดที่ไม่คาดคิด ลองใหม่อีกครั้งนะ"
            else:
                if sources:
                    result = replace(result, sources=sources, searched=True)
                ctx.ok, ctx.answer = True, result.text
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

    async def _run(self, ctx: AnswerContext) -> None:
        """ถาม AI ตามข้อมูลใน ctx แล้วเก็บผลลัพธ์กลับลง ctx"""
        await self.ask_ai(ctx)

    def _chunks(self, ctx: AnswerContext) -> list[str]:
        text = ctx.header + ctx.answer + (f"\n{ctx.footer}" if ctx.footer else "")
        return split_message(text) or ["(AI ไม่ได้ส่งข้อความกลับมา)"]

    async def _deliver(self, ctx: AnswerContext, send: Sender) -> None:
        """ส่งคำตอบ (ตัดเป็นหลายข้อความถ้ายาว) พร้อมปุ่มใต้ข้อความสุดท้าย"""
        view = AnswerView(self, ctx)
        chunks = self._chunks(ctx)
        try:
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                msg = await send(chunk, view if last else None)
                ctx.message_ids.append(msg.id)
                if last:
                    view.message = msg
        except discord.HTTPException:
            log.exception("Failed to send answer")
            view.stop()

    def _channel_sender(
        self, channel: discord.abc.Messageable, reply_to: discord.Message | None = None
    ) -> Sender:
        first = True

        async def send(content: str, view: discord.ui.View | None):
            nonlocal first
            kwargs = {"view": view} if view else {}
            if first and reply_to is not None:
                first = False
                return await reply_to.reply(content, allowed_mentions=REPLY_MENTIONS, **kwargs)
            first = False
            return await channel.send(content, **kwargs)

        return send

    @staticmethod
    def _followup_sender(interaction: discord.Interaction) -> Sender:
        async def send(content: str, view: discord.ui.View | None):
            kwargs = {"view": view} if view else {}
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
        chunks = self._chunks(ctx)
        single = len(chunks) == 1
        await interaction.edit_original_response(content=chunks[0], view=view if single else None)
        ctx.message_ids = [source_id] if source_id else []
        for i, chunk in enumerate(chunks[1:], start=1):
            last = i == len(chunks) - 1
            kwargs = {"view": view} if last else {}
            msg = await interaction.followup.send(chunk, wait=True, **kwargs)
            ctx.message_ids.append(msg.id)
            if last:
                view.message = msg

    async def continue_answer(self, interaction: discord.Interaction, view: AnswerView) -> None:
        if wait_msg := self._limit_message(interaction.user):
            await interaction.response.send_message(wait_msg, ephemeral=True)
            return
        view.busy = True
        try:
            await self._continue(interaction, view)
        finally:
            view.busy = False

    async def _continue(self, interaction: discord.Interaction, view: AnswerView) -> None:
        old = view.ctx
        await interaction.response.defer()
        # ปุ่ม "เขียนต่อ" ของคำตอบเดิมไม่ต้องใช้แล้ว
        view.remove_item(view.continue_)
        try:
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            pass
        ctx = AnswerContext(
            channel=old.channel, channel_id=old.channel_id, asker_id=old.asker_id,
            asker_name=old.asker_name, question=CONTINUE_QUESTION, images=(), header="",
            guild_id=old.guild_id, exempt=old.exempt,
        )
        async with ctx.channel.typing():
            await self._run(ctx)
        await self._deliver(ctx, self._followup_sender(interaction))

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
        # ถ้าข้อความนี้ reply ข้อความอื่น ให้ AI เห็นข้อความ/รูปนั้นด้วย
        quote, quoted_attachments = await self._replied_context(message)
        attachments = [*message.attachments, *quoted_attachments]
        has_image = self.config.max_images > 0 and any(map(is_image, attachments))
        if not question and not has_image and not quote:
            # ในห้องคุยกับ AI ข้อความที่มีแต่สติกเกอร์/ไฟล์อื่น ให้ข้ามไปเงียบ ๆ
            if not in_ai_channel:
                await message.reply("สวัสดี! พิมพ์คำถามต่อจากการ mention ได้เลย หรือใช้ `/ask` ก็ได้ 😊")
            return

        if wait_msg := self._limit_message(message.author):
            await message.reply(wait_msg, delete_after=10)
            return

        images: list[ImageData] = []
        if has_image:
            images, skipped = await read_images(
                attachments, self.config.max_images, self.config.max_image_bytes
            )
            if skipped:
                await message.reply(
                    "📷 ข้ามรูปเหล่านี้: " + ", ".join(skipped), delete_after=20,
                    allowed_mentions=REPLY_MENTIONS,
                )
            if not images and not question and not quote:
                return

        typed = question
        if not question:
            question = IMAGE_ONLY_QUESTION if images and not quote else REPLY_ONLY_QUESTION
        ctx = AnswerContext(
            channel=message.channel, channel_id=message.channel.id,
            asker_id=message.author.id, asker_name=message.author.display_name,
            question=f"{quote}\n{question}" if quote else question,
            images=tuple(images), header="",
            guild_id=message.guild.id if message.guild else None,
            exempt=self._is_exempt(message.author), search_query=typed,
        )
        await self._react(message, REACT_THINKING)
        # แสดง "กำลังพิมพ์..." ระหว่างรอ AI
        async with message.channel.typing():
            await self._run(ctx)
        await self._unreact(message, REACT_THINKING)
        if not ctx.ok:
            await self._react(message, REACT_ERROR)
        await self._deliver(ctx, self._channel_sender(message.channel, reply_to=message))

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

        parts = [replied.content.strip()]
        # ข้อความจากบอทอื่นมักอยู่ใน embed
        for embed in replied.embeds[:2]:
            parts += [embed.title or "", embed.description or ""]
        text = "\n".join(p for p in parts if p)[:1500]
        images = [a for a in replied.attachments if is_image(a)]
        if not text and not images:
            return "", []
        who = replied.author.display_name
        quote = (
            f'[ข้อความที่ถูกตอบกลับ จาก {who}]: "{text}"' if text
            else f"[รูปที่ถูกตอบกลับ จาก {who}]"
        )
        return quote, images

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
        @app_commands.describe(question="คำถามของคุณ", image="(ไม่บังคับ) แนบรูปให้ AI ดู")
        async def ask(
            interaction: discord.Interaction,
            question: str,
            image: discord.Attachment | None = None,
        ) -> None:
            if wait_msg := self._limit_message(interaction.user):
                await interaction.response.send_message(wait_msg, ephemeral=True)
                return

            # defer: บอก Discord ว่ากำลังประมวลผล (ขึ้น "กำลังคิด...") และขยายเวลาตอบจาก 3 วินาทีเป็น 15 นาที
            await interaction.response.defer(thinking=True)
            images: list[ImageData] = []
            note = ""
            if image is not None:
                if self.config.max_images == 0:
                    note = "\n-# 📷 ผู้ดูแลปิดการอ่านรูปไว้"
                else:
                    images, skipped = await read_images(
                        [image], self.config.max_images, self.config.max_image_bytes
                    )
                    if skipped:
                        note = "\n-# 📷 ข้ามรูป: " + ", ".join(skipped)

            # แสดงคำถามด้วย เพราะคนอื่นในช่องจะไม่เห็นว่าถามอะไร
            attached = " 📷" if images else ""
            header = f"> **{interaction.user.display_name}:** {question[:300]}{attached}{note}\n\n"
            ctx = AnswerContext(
                channel=interaction.channel,  # type: ignore[arg-type]
                channel_id=interaction.channel_id or interaction.user.id,
                asker_id=interaction.user.id, asker_name=interaction.user.display_name,
                question=question, images=tuple(images), header=header,
                guild_id=interaction.guild_id, exempt=self._is_exempt(interaction.user),
                search_query=question,
            )
            await self._run(ctx)
            await self._deliver(ctx, self._followup_sender(interaction))

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

            if mode.value == "on":
                self.ai_channels.enable(channel_id)
                await interaction.response.send_message(embed=self._welcome_embed())
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


    def _stats_embed(self, guild_id: int | None) -> discord.Embed:
        today = self._today()
        s = self.db.day_stats(today, guild_id)
        embed = discord.Embed(title="📈 สถิติบอท AI วันนี้", color=BRAND_COLOR)
        embed.add_field(name="💬 คำตอบ", value=f"**{s.answers:,}**", inline=True)
        embed.add_field(name="👥 ผู้ใช้", value=f"**{s.users:,}** คน", inline=True)
        embed.add_field(name="⚡ เวลาเฉลี่ย", value=f"**{s.avg_elapsed:.1f}** วิ", inline=True)
        embed.add_field(name="⚠️ error", value=f"{s.errors:,}", inline=True)
        embed.add_field(name="🛟 ใช้ตัวสำรอง", value=f"{s.backup:,}", inline=True)
        embed.add_field(name="🔎 ค้นเว็บ", value=f"{s.searched:,}", inline=True)

        top = self.db.top_users(today, guild_id)
        medals = ["🥇", "🥈", "🥉", "4.", "5."]
        embed.add_field(
            name="🏆 ถามมากที่สุดวันนี้",
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
        now = datetime.now(ZoneInfo(self.config.timezone))
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
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

    def _welcome_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💬 ห้องคุยกับ AI",
            description=(
                "พิมพ์คุยได้เลย **ไม่ต้องใช้ `/ask` หรือ mention** บอทจะตอบทุกข้อความในห้องนี้\n"
                "ห้องอื่นใช้ `/ask` หรือ `@บอท คำถาม` ได้เหมือนเดิม"
            ),
            color=BRAND_COLOR,
        )
        if self.config.max_images:
            embed.add_field(
                name="📷 ส่งรูปได้",
                value=f"แนบรูปพร้อมคำถาม (png / jpg / webp สูงสุด {self.config.max_images} รูป)",
                inline=False,
            )
        embed.add_field(
            name="🔘 ปุ่มใต้คำตอบ",
            value="🔄 ตอบใหม่ · ➡️ เขียนต่อ · 🗑️ ลบ (ใช้ได้ 10 นาที เฉพาะคนถาม)",
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
