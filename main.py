"""Discord AI Chatbot — จุดเริ่มต้นโปรแกรม

รัน: python main.py
"""

from __future__ import annotations

import logging
import re
import sys

import discord
from discord import app_commands

from bot.config import Config, ConfigError
from bot.cooldown import UserCooldown
from bot.memory import ChannelMemory
from bot.providers import AIError, AIProvider, create_provider
from bot.utils import split_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ห้ามคำตอบของ AI ไป ping @everyone / @here / role / ผู้ใช้คนอื่น
SAFE_MENTIONS = discord.AllowedMentions.none()
# ตอนตอบกลับข้อความ ให้แจ้งเตือนเฉพาะคนที่ถาม
REPLY_MENTIONS = discord.AllowedMentions(
    everyone=False, users=False, roles=False, replied_user=True
)


class AIChatBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # ต้องเปิด Message Content Intent ใน Developer Portal ด้วย
        super().__init__(intents=intents, allowed_mentions=SAFE_MENTIONS)

        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.memory = ChannelMemory(config.memory_size)
        self.cooldown = UserCooldown(config.user_cooldown)
        self.ai: AIProvider = create_provider(config)
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

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (ID %s) | provider=%s model=%s",
            self.user, self.user.id if self.user else "?", self.config.provider, self.config.model,
        )
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="/ask หรือ @mention")
        )

    async def close(self) -> None:
        await self.ai.close()
        await super().close()

    # ---------- core ----------

    async def ask_ai(self, channel_id: int, author: str, question: str) -> str:
        """ส่งคำถามไปยัง AI พร้อมบริบทของช่อง คืนคำตอบ (หรือข้อความแจ้ง error)"""
        # ใส่ชื่อผู้ถาม เพราะในช่องเดียวอาจมีหลายคนคุยกับบอท
        prompt = f"{author}: {question}"
        async with self.memory.lock(channel_id):
            history = self.memory.get(channel_id)
            try:
                answer = await self.ai.generate(history, prompt)
            except AIError as e:
                log.warning("AI error in channel %s: %r", channel_id, e)
                return e.user_message
            except Exception:
                log.exception("Unexpected error while calling AI")
                return "⚠️ เกิดข้อผิดพลาดที่ไม่คาดคิด ลองใหม่อีกครั้งนะ"
            self.memory.add_exchange(channel_id, prompt, answer)
            return answer

    def _cooldown_message(self, user_id: int) -> str | None:
        remaining = self.cooldown.check(user_id)
        if remaining > 0:
            return f"🕒 ใจเย็น ๆ นะ รออีก {remaining:.0f} วินาทีแล้วค่อยถามใหม่"
        return None

    # ---------- mention ----------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        if self.user not in message.mentions:
            return

        # ตัด mention ของบอทออกจากข้อความ
        question = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        if not question:
            await message.reply("สวัสดี! พิมพ์คำถามต่อจากการ mention ได้เลย หรือใช้ `/ask` ก็ได้ 😊")
            return

        if wait_msg := self._cooldown_message(message.author.id):
            await message.reply(wait_msg, delete_after=10)
            return

        # แสดง "กำลังพิมพ์..." ระหว่างรอ AI
        async with message.channel.typing():
            answer = await self.ask_ai(message.channel.id, message.author.display_name, question)

        await self._send_reply(message, answer)

    async def _send_reply(self, message: discord.Message, text: str) -> None:
        chunks = split_message(text) or ["(AI ไม่ได้ส่งข้อความกลับมา)"]
        try:
            await message.reply(chunks[0], allowed_mentions=REPLY_MENTIONS)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        except discord.HTTPException:
            log.exception("Failed to send reply")

    # ---------- slash commands ----------

    def _register_commands(self) -> None:
        @self.tree.command(name="ask", description="ถามคำถามกับ AI")
        @app_commands.describe(question="คำถามของคุณ")
        async def ask(interaction: discord.Interaction, question: str) -> None:
            if wait_msg := self._cooldown_message(interaction.user.id):
                await interaction.response.send_message(wait_msg, ephemeral=True)
                return

            # defer: บอก Discord ว่ากำลังประมวลผล (ขึ้น "กำลังคิด...") และขยายเวลาตอบจาก 3 วินาทีเป็น 15 นาที
            await interaction.response.defer(thinking=True)
            channel_id = interaction.channel_id or interaction.user.id
            answer = await self.ask_ai(channel_id, interaction.user.display_name, question)

            # แสดงคำถามด้วย เพราะคนอื่นในช่องจะไม่เห็นว่าถามอะไร
            header = f"> **{interaction.user.display_name}:** {question[:300]}\n\n"
            chunks = split_message(header + answer)
            try:
                await interaction.followup.send(chunks[0])
                for chunk in chunks[1:]:
                    await interaction.followup.send(chunk)
            except discord.HTTPException:
                log.exception("Failed to send followup")

        @self.tree.command(name="reset", description="ล้างความจำบทสนทนาของ AI ในช่องนี้")
        async def reset(interaction: discord.Interaction) -> None:
            channel_id = interaction.channel_id or interaction.user.id
            self.memory.reset(channel_id)
            await interaction.response.send_message("🧹 ล้างความจำของช่องนี้แล้ว เริ่มคุยใหม่ได้เลย!")

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


def main() -> None:
    try:
        config = Config.load()
    except ConfigError as e:
        log.error("ตั้งค่าไม่ถูกต้อง: %s (ดูตัวอย่างใน .env.example)", e)
        sys.exit(1)

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
