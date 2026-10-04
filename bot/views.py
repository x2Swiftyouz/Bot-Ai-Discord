"""ปุ่มใต้คำตอบของ AI: 🔄 ตอบใหม่ · ➡️ เขียนต่อ · 🗑️ ลบ"""

from __future__ import annotations

import logging
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
        self.clear_items()
        self.add_item(self.regenerate)
        if self.ctx.ok:
            self.add_item(self.continue_)
        self.add_item(self.delete)

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

    @discord.ui.button(label="ตอบใหม่", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def regenerate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.regenerate_answer(interaction, self)

    @discord.ui.button(label="เขียนต่อ", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def continue_(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.continue_answer(interaction, self)

    @discord.ui.button(label="ลบ", emoji="🗑️", style=discord.ButtonStyle.secondary)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.delete_answer(interaction, self)
