"""ทดสอบการทำงานของบอทกับ Discord จำลอง (ไม่ต่ออินเทอร์เน็ต)"""

import itertools
import types

import discord
from conftest import run

import main
from bot.providers import AIResult

_ids = itertools.count(1000)


class FakeTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return None


class FakeMessage:
    def __init__(self, log, content="", channel=None, attachments=()):
        self.id = next(_ids)
        self.log = log
        self.content = content
        self.channel = channel
        self.attachments = list(attachments)
        self.author = types.SimpleNamespace(bot=False, id=7, display_name="ปีเตอร์")
        self.type = discord.MessageType.default
        self.mentions = []
        self.reference = None
        self.guild = None

    async def reply(self, content, **kwargs):
        self.log.append(("reply", content, bool(kwargs.get("view"))))
        return FakeMessage(self.log, content)

    async def edit(self, **kwargs):
        self.log.append(("edit", kwargs.get("content")))

    async def add_reaction(self, emoji):
        self.log.append(("react", emoji))

    async def remove_reaction(self, emoji, user):
        pass

    async def create_thread(self, name, **kwargs):
        self.log.append(("thread", name))
        return FakeChannel(self.log, 999, parent_id=self.channel.id)


class FakeChannel:
    def __init__(self, log, channel_id=111, parent_id=None):
        self.log = log
        self.id = channel_id
        self.parent_id = parent_id

    def typing(self):
        return FakeTyping()

    async def send(self, content, **kwargs):
        self.log.append(("send", content, bool(kwargs.get("view"))))
        return FakeMessage(self.log, content)

    async def edit(self, **kwargs):
        self.log.append(("rename", kwargs.get("name")))

    def get_partial_message(self, message_id):
        log = self.log

        async def delete():
            log.append(("delete", message_id))

        async def edit(**kwargs):
            log.append(("edit", kwargs.get("content"), bool(kwargs.get("view"))))
            return FakeMessage(log, kwargs.get("content"))

        return types.SimpleNamespace(delete=delete, edit=edit)


class FakeAI:
    def __init__(self, text="นี่คือคำตอบ"):
        self.text = text
        self.prompts = []

    async def generate(self, history, prompt, images=(), on_delta=None, retry=True):
        self.prompts.append(prompt)
        return AIResult(self.text, "fake-model")

    async def check_models(self):
        return []

    async def close(self):
        pass


def make_bot(env, **extra):
    env(AI_CHANNEL_IDS="111", **extra)
    bot = main.AIChatBot(main.Config.load())
    bot.ai = FakeAI()
    bot._connection.user = types.SimpleNamespace(id=42)
    return bot


def test_ai_channel_reply_and_ignore_prefix(env):
    bot = make_bot(env)
    log = []
    channel = FakeChannel(log)

    async def scenario():
        await bot.on_message(FakeMessage(log, "สวัสดี", channel))
        await bot.on_message(FakeMessage(log, "// คุยกันเอง", channel))
        await bot.on_message(FakeMessage(log, "ไม่ใช่ห้อง AI", FakeChannel(log, 222)))

    run(scenario())
    replies = [entry for entry in log if entry[0] == "reply"]
    assert len(replies) == 1 and replies[0][1].startswith("นี่คือคำตอบ") and replies[0][2]
    assert bot.ai.prompts == ["ปีเตอร์: สวัสดี"]
    bot.db.close()


def test_remember_prefix_saves_note_without_ai(env):
    bot = make_bot(env)
    log = []

    async def scenario():
        await bot.on_message(FakeMessage(log, "จำไว้ว่า ฉันชอบส้มตำ", FakeChannel(log)))

    run(scenario())
    assert bot.db.notes(7) == ["ฉันชอบส้มตำ"]
    assert bot.ai.prompts == []
    bot.db.close()


def test_daily_quota(env):
    bot = make_bot(env, DAILY_LIMIT_PER_USER="2")
    log = []
    channel = FakeChannel(log)

    async def scenario():
        for _ in range(3):
            await bot.on_message(FakeMessage(log, "คำถาม", channel))

    run(scenario())
    assert len(bot.ai.prompts) == 2
    assert any("ครบ 2 คำถาม" in entry[1] for entry in log if entry[0] == "reply")
    bot.db.close()


def test_thread_mode_opens_thread_and_renames(env):
    bot = make_bot(env)
    bot.db.set_setting("thread_mode:111", "1")
    bot._thread_mode = lambda channel: channel.parent_id is None
    log = []

    async def scenario():
        await bot.on_message(FakeMessage(log, "แนะนำเกมมือถือหน่อย", FakeChannel(log)))
        for task in list(bot._background):
            await task

    run(scenario())
    kinds = [entry[0] for entry in log if entry[0] != "react"]
    assert kinds[:3] == ["thread", "send", "send"]  # เปิดเธรด → ข้อความแนะนำ + ปุ่มปิด → คำตอบ
    assert ("rename", "💬 นี่คือคำตอบ") in log
    bot.db.close()


def test_long_answer_keeps_tail_in_one_message(env):
    bot = make_bot(env, LONG_ANSWER_FILE_CHARS="2000")
    ctx = main.AnswerContext(channel=None, channel_id=1, asker_id=1, asker_name="x", question="q",
                             images=(), header="")
    ctx.ok = True
    ctx.answer = "| ตัวเลือก | ค่า |\n|---|---|\n" + "| Gyro | 300 |\n" * 200
    ctx.extras = "\n\n🎬 **คลิปที่เจอ**\n" + "\n".join(f"{i}. [คลิป {i}](<https://youtube.com/{i}>)" for i in range(5))
    ctx.footer = "-# ⚡ 1.0 วิ · fake-model"
    chunks, file = bot._render(ctx)
    assert len(chunks) == 1 and len(chunks[0]) <= 2000
    assert "|" not in chunks[0].split("🎬")[0]
    assert "🎬" in chunks[0] and "⚡" in chunks[0]
    assert file is not None and file.filename == "answer.txt"
    bot.db.close()


def test_answer_buttons(env):
    bot = make_bot(env)

    async def scenario():
        ok = main.AnswerContext(channel=None, channel_id=1, asker_id=1, asker_name="x", question="q",
                                images=(), header="")
        ok.ok, ok.answer = True, "ภาษาไทย"
        failed = main.AnswerContext(channel=None, channel_id=1, asker_id=1, asker_name="x", question="q",
                                    images=(), header="")
        return ([b.label for b in main.AnswerView(bot, ok).children],
                [b.label for b in main.AnswerView(bot, failed).children])

    good, bad = run(scenario())
    assert good == ["ตอบใหม่", "เขียนต่อ", "ลบ", "สั้นลง", "ละเอียดขึ้น", "แปลอังกฤษ"]
    assert bad == ["ลองใหม่", "ลบ"]
    bot.db.close()


def test_edit_question_updates_answer(env):
    bot = make_bot(env)  # cooldown ปกติ 10 วิ — แก้คำถามทันทีต้องไม่ติด cooldown
    log = []
    channel = FakeChannel(log)

    async def scenario():
        question = FakeMessage(log, "เมืองหลวงของญี่ปุ่น", channel)
        await bot.on_message(question)
        bot.ai.text = "โตเกียว"
        before = FakeMessage(log, question.content, channel)
        question.content = "เมืองหลวงของเกาหลีใต้"
        await bot.on_message_edit(before, question)
        # เนื้อหาเท่าเดิม (เช่น Discord โหลดพรีวิวลิงก์) → ไม่ตอบซ้ำ
        await bot.on_message_edit(question, question)

    run(scenario())
    assert bot.ai.prompts == ["ปีเตอร์: เมืองหลวงของญี่ปุ่น", "ปีเตอร์: เมืองหลวงของเกาหลีใต้"]
    edits = [entry for entry in log if entry[0] == "edit"]
    assert len(edits) == 1 and edits[0][1].startswith(main.EDITED_HEADER + "โตเกียว") and edits[0][2]
    # ความจำของห้องเหลือแค่คำถามใหม่
    assert [m.content for m in bot.memory.get(111)] == ["ปีเตอร์: เมืองหลวงของเกาหลีใต้", "โตเกียว"]
    bot.db.close()
