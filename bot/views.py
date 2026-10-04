"""ปุ่มใต้คำตอบของ AI: 🔄 ตอบใหม่ · ➡️ เขียนต่อ · 🗑️ ลบ"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from .providers import ImageData

if TYPE_CHECKING:
    from main import AIChatBot

log = logging.getLogger(__name__)

# ปุ่มใช้ได้ 10 นาทีหลังคำตอบล่าสุด แล้วจะหายไปเอง (กันปุ่มค้างที่กดไม่ได้หลังรีสตาร์ท)
BUTTON_TIMEOUT = 600


@dataclass
class AnswerContext:
    """ข้อมูลของคำตอบหนึ่งชุด เก็บไว้ให้ปุ่มใช้"""

    channel: discord.abc.Messageable
    channel_id: int
    asker_id: int
    asker_name: str
    question: str
    images: tuple[ImageData, ...]
    header: str  # ข้อความนำหน้าคำตอบ (เช่น คำถามของ /ask)
    guild_id: int | None = None
    exempt: bool = False  # แอดมิน ไม่ถูกจำกัดโควต้ารายวัน
    search_query: str = ""  # ข้อความที่ผู้ใช้พิมพ์จริง ใช้ตัดสินใจ/ค้นเว็บ (ว่าง = ไม่ค้น)
    attachments_text: str = ""  # เนื้อหาไฟล์ที่แนบมา (ส่งให้ AI แต่ไม่เก็บลงความจำ)
    file_names: tuple[str, ...] = ()
    use_memory: bool = True  # False = คำถามเดี่ยว ไม่อ่าน/ไม่บันทึกความจำของช่อง (เช่น เมนูคลิกขวา)
    ok: bool = False
    prompt: str = ""
    answer: str = ""
    footer: str = ""  # บรรทัดเล็กใต้คำตอบ (ไม่เก็บลงความจำ)
    message_ids: list[int] = field(default_factory=list)


class AnswerView(discord.ui.View):
    def __init__(self, bot: AIChatBot, ctx: AnswerContext) -> None:
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.bot = bot
        self.ctx = ctx
        self.message: discord.Message | discord.WebhookMessage | None = None
        self.busy = False
        self.refresh()

    def refresh(self) -> None:
        """คำตอบที่ error จะเหลือแค่ปุ่ม "ลองใหม่" กับ "ลบ" """
        self.regenerate.label = "ตอบใหม่" if self.ctx.ok else "ลองใหม่"
        # แปลไปอีกภาษา: คำตอบเป็นไทย → อังกฤษ, ไม่ใช่ไทย → ไทย
        self.translate.label = "แปลอังกฤษ" if self.answer_is_thai else "แปลไทย"
        self.clear_items()
        self.add_item(self.regenerate)
        if self.ctx.ok:
            self.add_item(self.continue_)
        self.add_item(self.delete)
        if self.ctx.ok:
            # แถวที่ 2: ปุ่มคำถามแนะนำ ปรับคำตอบได้ในคลิกเดียว
            for item in (self.shorter, self.detail, self.translate):
                self.add_item(item)

    @property
    def answer_is_thai(self) -> bool:
        return bool(re.search(r"[\u0E00-\u0E7F]", self.ctx.answer))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        allowed = user.id == self.ctx.asker_id
        if not allowed and isinstance(user, discord.Member) and interaction.channel is not None:
            perms = interaction.channel.permissions_for(user)  # type: ignore[union-attr]
            allowed = perms.manage_messages
        if not allowed:
            await interaction.response.send_message(
                f"ปุ่มนี้ใช้ได้เฉพาะ **{self.ctx.asker_name}** ที่เป็นคนถามนะ 🙏", ephemeral=True
            )
            return False
        if self.busy:
            await interaction.response.send_message("⏳ กำลังทำงานอยู่ รอสักครู่นะ", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("Button error", exc_info=error)
        self.busy = False
        try:
            msg = "⚠️ เกิดข้อผิดพลาด ลองใหม่อีกครั้งนะ"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="ตอบใหม่", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def regenerate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.regenerate_answer(interaction, self)

    @discord.ui.button(label="เขียนต่อ", emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def continue_(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.followup_answer(interaction, self, "continue")

    @discord.ui.button(label="ลบ", emoji="🗑️", style=discord.ButtonStyle.secondary, row=0)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.delete_answer(interaction, self)

    @discord.ui.button(label="สั้นลง", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
    async def shorter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.followup_answer(interaction, self, "shorter")

    @discord.ui.button(label="ละเอียดขึ้น", emoji="📖", style=discord.ButtonStyle.secondary, row=1)
    async def detail(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.followup_answer(interaction, self, "detail")

    @discord.ui.button(label="แปลอังกฤษ", emoji="🌐", style=discord.ButtonStyle.secondary, row=1)
    async def translate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.followup_answer(
            interaction, self, "to_english" if self.answer_is_thai else "to_thai"
        )


class CloseThreadView(discord.ui.View):
    """ปุ่ม 🔒 ปิดเธรด ในเธรดที่บอทเปิดให้ (โหมดเธรด)

    เป็นปุ่มถาวร (timeout=None + custom_id คงที่) จึงยังกดได้แม้บอทรีสตาร์ท
    ปิด = archive เธรด ถ้ามีคนพิมพ์ในเธรดอีก Discord จะเปิดเธรดกลับมาเอง
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="ปิดเธรด", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="ai:close_thread"
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("ปุ่มนี้ใช้ได้ในเธรดเท่านั้น", ephemeral=True)
            return
        user = interaction.user
        allowed = isinstance(user, discord.Member) and thread.permissions_for(user).manage_threads
        if not allowed and thread.parent is not None:
            # เธรดที่เปิดจากข้อความ มี id เดียวกับข้อความเริ่มต้น → คนถามคนแรกปิดได้
            try:
                starter = await thread.parent.fetch_message(thread.id)  # type: ignore[union-attr]
                allowed = starter.author.id == user.id
            except discord.HTTPException:
                pass
        if not allowed:
            await interaction.response.send_message(
                "ปิดเธรดได้เฉพาะคนที่เริ่มถาม หรือคนที่มีสิทธิ์ Manage Threads นะ", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🔒 {user.display_name} ปิดเธรดนี้แล้ว — พิมพ์ในเธรดเพื่อคุยต่อได้ทุกเมื่อ"
        )
        try:
            await thread.edit(archived=True)
        except discord.HTTPException as e:
            log.warning("ปิดเธรดไม่ได้: %r", e)
