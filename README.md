# 🤖 Bot-Ai-Discord

บอท Discord ที่เป็น AI แชทบอท ใช้ **AI API ฟรี** (Google Gemini เป็นตัวหลัก สลับไป Groq หรือ OpenRouter ได้ผ่าน `.env`) รองรับภาษาไทย

## ✨ ฟีเจอร์

| ฟีเจอร์ | รายละเอียด |
|---|---|
| ถามได้ 2 แบบ | `@ชื่อบอท คำถาม` หรือ `/ask question:คำถาม` |
| จำบทสนทนา | จำ 10 ข้อความล่าสุด **แยกตามช่อง** (ปรับได้ด้วย `MEMORY_SIZE`) |
| ล้างความจำ | `/reset` ล้างความจำเฉพาะช่องนั้น |
| ข้อความยาว | ตัดเป็นหลายข้อความอัตโนมัติ (ไม่เกิน 2000 ตัวอักษร) ถ้าตัดกลาง code block จะปิด/เปิด ``` ให้เอง |
| สถานะรอ | ขึ้น "กำลังพิมพ์..." ตอน mention / "กำลังคิด..." ตอนใช้ `/ask` |
| บุคลิกบอท | ตั้งได้จาก `SYSTEM_PROMPT` ใน `.env` |
| ความเสถียร | API ล่ม, timeout, โดน rate limit (429), key ผิด → บอทตอบข้อความแจ้งผู้ใช้ ไม่ crash |
| ลองใหม่อัตโนมัติ | เซิร์ฟเวอร์ AI ล่มชั่วคราว (503) → ลองซ้ำเอง และสลับไป **โมเดลสำรอง** ได้ถ้าตั้งไว้ |
| กันเกินโควต้า | cooldown ต่อผู้ใช้ (`USER_COOLDOWN_SECONDS`) ใช้ร่วมกันทั้ง mention และ `/ask` |
| ปลอดภัย | ความลับอยู่ใน `.env` เท่านั้น, คำตอบของ AI ไม่สามารถ ping `@everyone`/`@here`/role ได้ |

## 🧰 ทำไมเลือก Python + discord.py

- **อ่านง่าย ติดตั้งน้อย** — ใช้แค่ 2 แพ็กเกจ (`discord.py`, `python-dotenv`) ส่วน `aiohttp` ที่ใช้เรียก AI API ติดมากับ discord.py อยู่แล้ว
- **async ทั้งหมด** — discord.py สร้างบน asyncio โดยตรง การเรียก AI ใช้ `aiohttp` แบบ async จึงไม่บล็อกบอทระหว่างรอคำตอบ
- **ไม่ผูกกับ SDK ของเจ้าใดเจ้าหนึ่ง** — เรียก REST API ตรง ๆ ทำให้สลับ Gemini / Groq / OpenRouter ได้ด้วยการแก้ `.env` บรรทัดเดียว และไม่พังเมื่อ SDK เปลี่ยนเวอร์ชัน
- discord.js v14 ก็ทำได้เท่ากัน แต่ต้องติดตั้งแพ็กเกจและตั้งค่าไฟล์มากกว่า (เช่น สคริปต์ deploy คำสั่งแยก) จึงเลือก Python เพื่อให้มือใหม่เริ่มได้เร็วกว่า

## 📁 โครงสร้างโฟลเดอร์

```
Bot-Ai-Discord/
├── main.py              # จุดเริ่มต้น: ตัวบอท, การ mention, /ask, /reset
├── bot/
│   ├── __init__.py
│   ├── config.py        # อ่านและตรวจสอบค่าจาก .env
│   ├── providers.py     # เชื่อมต่อ Gemini / Groq / OpenRouter + จัดการ error
│   ├── memory.py        # ความจำบทสนทนาแยกตามช่อง
│   ├── cooldown.py      # cooldown ต่อผู้ใช้
│   └── utils.py         # ตัดข้อความยาวเกิน 2000 ตัวอักษร
├── requirements.txt
├── .env.example         # ตัวอย่างไฟล์ตั้งค่า (คัดลอกเป็น .env)
├── .gitignore           # กันไม่ให้ .env หลุดขึ้น Git
└── README.md
```

---

# 📖 คู่มือติดตั้งทีละขั้นตอน

## ขั้นที่ 1: สร้างบอทใน Discord Developer Portal

1. เข้า <https://discord.com/developers/applications> แล้วล็อกอินด้วยบัญชี Discord
2. กด **New Application** → ตั้งชื่อบอท → ติ๊กยอมรับเงื่อนไข → **Create**
3. เมนูซ้ายเลือก **Bot**
4. กด **Reset Token** → **Yes, do it!** → กด **Copy** เก็บ token ไว้ (จะเห็นแค่ครั้งเดียว)
   > ⚠️ **token = รหัสผ่านของบอท** ห้ามแชร์ ห้ามโพสต์ ห้าม commit ขึ้น GitHub ถ้าหลุดให้กด Reset Token ทันที

## ขั้นที่ 2: เปิด Message Content Intent

ยังอยู่ที่หน้า **Bot** เลื่อนลงไปหัวข้อ **Privileged Gateway Intents**

1. เปิดสวิตช์ **MESSAGE CONTENT INTENT** ✅ (จำเป็น ไม่งั้นบอทอ่านข้อความตอน mention ไม่ได้)
2. กด **Save Changes**

> ถ้าลืมขั้นนี้ ตอนรันบอทจะขึ้น error ว่า `ยังไม่ได้เปิด Message Content Intent`

## ขั้นที่ 3: เชิญบอทเข้าเซิร์ฟเวอร์

1. เมนูซ้ายเลือก **OAuth2** → **URL Generator**
2. ช่อง **Scopes** ติ๊ก: `bot` และ `applications.commands`
3. ช่อง **Bot Permissions** ติ๊ก:
   - `View Channels`
   - `Send Messages`
   - `Send Messages in Threads`
   - `Read Message History`
4. คัดลอกลิงก์ด้านล่างสุด (Generated URL) ไปเปิดในเบราว์เซอร์
5. เลือกเซิร์ฟเวอร์ที่ต้องการ (ต้องมีสิทธิ์ **Manage Server**) → **Authorize**

**(แนะนำ) หา Server ID เพื่อให้ slash command ขึ้นทันที:**
Discord → User Settings → Advanced → เปิด **Developer Mode** → คลิกขวาที่ไอคอนเซิร์ฟเวอร์ → **Copy Server ID** → ใส่ใน `GUILD_ID` ของ `.env`

## ขั้นที่ 4: ขอ API key ฟรี (ไม่ต้องผูกบัตรเครดิต)

เลือกอย่างน้อย 1 เจ้า (แนะนำ Gemini)

### 🔷 Google Gemini (ตัวหลัก)
1. เข้า <https://aistudio.google.com/apikey> ล็อกอินด้วยบัญชี Google
2. กด **Create API key** → คัดลอก key
3. ใส่ใน `.env`: `AI_PROVIDER=gemini`, `GEMINI_API_KEY=...`
4. ตรวจชื่อโมเดลล่าสุดที่ <https://ai.google.dev/gemini-api/docs/models> แล้วใส่ใน `GEMINI_MODEL` (เช่น `gemini-3.8-flash`)
   - ดูโควต้าฟรีของแต่ละโมเดลได้ที่ <https://ai.google.dev/gemini-api/docs/rate-limits>
   - หมายเหตุ: ข้อมูลที่ส่งผ่าน free tier อาจถูก Google นำไปใช้ปรับปรุงบริการ อย่าให้ผู้ใช้ส่งข้อมูลส่วนตัว/ความลับ

### 🟧 Groq (เร็วมาก)
1. เข้า <https://console.groq.com/keys> → สมัคร/ล็อกอิน → **Create API Key**
2. ใส่ใน `.env`: `AI_PROVIDER=groq`, `GROQ_API_KEY=...`
3. เลือกโมเดลจาก <https://console.groq.com/docs/models> ใส่ใน `GROQ_MODEL`

### 🟪 OpenRouter (รวมหลายโมเดล)
1. เข้า <https://openrouter.ai/keys> → สมัคร/ล็อกอิน → **Create Key**
2. ใส่ใน `.env`: `AI_PROVIDER=openrouter`, `OPENROUTER_API_KEY=...`
3. เลือกโมเดลฟรีจาก <https://openrouter.ai/models?max_price=0> — **ต้องเป็นชื่อที่ลงท้าย `:free`** เท่านั้น ถึงจะไม่เสียเงิน
   - บัญชีที่ไม่ได้เติมเงิน จะมีโควต้าต่อวันของโมเดล `:free` ค่อนข้างจำกัด

> 💡 **ทำไมชื่อโมเดลอยู่ใน `.env`:** โมเดลฟรีถูกเพิ่ม/ถอดบ่อย ถ้าวันหนึ่งบอทตอบว่า "🧩 ไม่พบโมเดล AI" และ log ขึ้น HTTP 404 แปลว่าโมเดลถูกถอดแล้ว (ข้อความ error ใน log มักบอกชื่อโมเดลใหม่ที่แนะนำ) — แค่เปลี่ยนชื่อโมเดลใน `.env` แล้วรีสตาร์ท ไม่ต้องแก้โค้ด

## ขั้นที่ 5: รันบนเครื่อง

**ต้องมี Python 3.10 ขึ้นไป** (ดาวน์โหลด: <https://www.python.org/downloads/> — บน Windows ติ๊ก "Add Python to PATH" ตอนติดตั้ง)

```bash
# 1) ดาวน์โหลดโค้ด
git clone https://github.com/x2Swiftyouz/Bot-Ai-Discord.git
cd Bot-Ai-Discord

# 2) สร้าง virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 3) ติดตั้งแพ็กเกจ
pip install -r requirements.txt

# 4) สร้างไฟล์ตั้งค่าจากตัวอย่าง แล้วเปิดแก้ใส่ token / API key
# Windows:
copy .env.example .env
# macOS / Linux:
cp .env.example .env

# 5) รันบอท
python main.py
```

ถ้าสำเร็จจะเห็นประมาณนี้:

```
[INFO] bot: Synced 2 command(s) to guild 123456789012345678
[INFO] bot: Logged in as MyBot#1234 (ID ...) | provider=gemini model=gemini-3.8-flash
```

ทดลองใน Discord:
- `@MyBot สวัสดี แนะนำตัวหน่อย`
- `/ask question: อธิบาย async ใน Python แบบเข้าใจง่าย`
- `/reset`

กด `Ctrl + C` เพื่อหยุดบอท

## ⚙️ ค่าตั้งค่าทั้งหมดใน `.env`

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `DISCORD_TOKEN` | — | token ของบอท (จำเป็น) |
| `GUILD_ID` | ว่าง | ID เซิร์ฟเวอร์ ให้ slash command ขึ้นทันที (ว่าง = global อาจรอนาน) |
| `AI_PROVIDER` | `gemini` | `gemini` / `groq` / `openrouter` |
| `<PROVIDER>_API_KEY` | — | key ของเจ้าที่เลือก (จำเป็นเฉพาะเจ้าที่ใช้) |
| `<PROVIDER>_MODEL` | — | ชื่อโมเดลของเจ้าที่เลือก |
| `<PROVIDER>_FALLBACK_MODELS` | ว่าง | โมเดลสำรอง คั่นด้วยจุลภาค เช่น `รุ่น-a,รุ่น-b` ใช้เมื่อโมเดลหลักล่ม/เกินโควต้า/ถูกถอด |
| `AI_MAX_RETRIES` | `2` | ลองใหม่กี่ครั้งเมื่อเซิร์ฟเวอร์ AI ล่มชั่วคราว (รอ 1, 2, 4 ... วินาที) ก่อนสลับไปโมเดลสำรอง |
| `SYSTEM_PROMPT` | ผู้ช่วยที่เป็นมิตร | บุคลิกของบอท |
| `MEMORY_SIZE` | `10` | จำนวนข้อความที่จำต่อช่อง (ผู้ใช้+บอท) |
| `USER_COOLDOWN_SECONDS` | `10` | ต้องรอกี่วินาทีก่อนถามครั้งถัดไป |
| `AI_TIMEOUT_SECONDS` | `60` | รอ AI ตอบนานสุดกี่วินาที |
| `AI_TEMPERATURE` | `0.7` | ความสร้างสรรค์ของคำตอบ |

## 🩺 แก้ปัญหาที่พบบ่อย

| อาการ | สาเหตุ / วิธีแก้ |
|---|---|
| `DISCORD_TOKEN ไม่ถูกต้อง` | คัดลอก token ผิด หรือ token ถูกรีเซ็ต → Reset Token ใหม่ |
| `ยังไม่ได้เปิด Message Content Intent` | ทำขั้นที่ 2 |
| ไม่เห็น `/ask` | ใส่ `GUILD_ID` แล้วรีสตาร์ท, ตรวจว่าเชิญบอทด้วย scope `applications.commands`, ลองกด `Ctrl+R` รีโหลด Discord |
| mention แล้วบอทเงียบ | บอทไม่มีสิทธิ์ View/Send ในช่องนั้น หรือยังไม่เปิด Message Content Intent |
| บอทตอบ "🔑 API key ไม่ถูกต้อง" | ตรวจ `*_API_KEY` ให้ตรงกับ `AI_PROVIDER` |
| บอทตอบ "🔥 เซิร์ฟเวอร์ AI มีคนใช้งานหนาแน่น" + log `HTTP 503` | ฝั่งผู้ให้บริการ AI คนใช้เยอะชั่วคราว ไม่ใช่ความผิดของบอท → รอสักพัก หรือใส่ `<PROVIDER>_FALLBACK_MODELS` เป็นโมเดลอื่น (เช่นรุ่นเล็กกว่า) ไว้สำรอง |
| บอทตอบ "⏳ เกินโควต้าฟรี" | โดน rate limit → รอสักพัก, เพิ่ม `USER_COOLDOWN_SECONDS`, หรือสลับไปเจ้าอื่น |
| บอทตอบ "🧩 ไม่พบโมเดล AI" + log `HTTP 404` | ชื่อโมเดลผิดหรือโมเดลถูกถอด → เปลี่ยน `*_MODEL` |
| log ขึ้น `PyNaCl is not installed, voice will NOT be supported` | เป็นแค่คำเตือนเรื่องระบบเสียง บอทนี้ไม่ใช้เสียง ไม่ต้องสนใจ |

> หมายเหตุ: ความจำเก็บในหน่วยความจำ (RAM) — รีสตาร์ทบอทแล้วความจำจะหายหมด

---

# ☁️ ที่โฮสต์บอทฟรีแบบ 24/7

บอท Discord ต้องเชื่อมต่อค้างไว้ตลอด (WebSocket) จึงต้องการเครื่องที่ **รันต่อเนื่อง ไม่หลับ** — บริการฟรีหลายเจ้าไม่เหมาะเพราะจะ "หลับ" เมื่อไม่มีคนเข้าเว็บ

> ⚠️ เงื่อนไขของบริการฟรีเปลี่ยนบ่อยมาก ตรวจสอบหน้า pricing ล่าสุดของแต่ละเจ้าก่อนใช้งานเสมอ

| ที่โฮสต์ | 24/7 จริง? | ต้องผูกบัตร? | ข้อจำกัด / หมายเหตุ |
|---|---|---|---|
| **คอมเก่า / Raspberry Pi ที่บ้าน** | ✅ | ❌ | ฟรีจริงและควบคุมได้เต็มที่ แต่ต้องเปิดเครื่องและเน็ตทิ้งไว้ เสียค่าไฟเล็กน้อย |
| **Oracle Cloud Always Free** | ✅ | ⚠️ ต้องใช้บัตรยืนยันตัวตน (ไม่ตัดเงินถ้าใช้แค่ Always Free) | VM ฟรีถาวร สเปกเหลือเฟือ แต่สมัครยาก บางครั้งเครื่องเต็มในบาง region, บัญชีที่ไม่ใช้งานนานอาจถูกเรียกคืนเครื่อง ต้องตั้งค่า Linux เอง |
| **Google Cloud (e2-micro free tier)** | ✅ | ⚠️ ต้องผูกบัตร | ฟรีเฉพาะ region ในสหรัฐฯ บาง region, RAM 1GB พอสำหรับบอทนี้ ระวังค่าใช้จ่ายถ้าเลือกสเปก/region ผิด |
| **Koyeb (free instance)** | ⚠️ | ขึ้นกับนโยบายปัจจุบัน | เครื่องฟรีสเปกเล็ก อาจถูก scale-to-zero เมื่อไม่มี traffic ต้องตรวจนโยบายล่าสุด |
| **Render (free web service)** | ❌ | ❌ | หลับหลังไม่มีคนเข้า ~15 นาที → บอท offline (Background Worker ต้องเสียเงิน) |
| **Railway** | ⚠️ | ❌ ช่วงทดลอง | ได้เครดิตทดลองจำนวนจำกัด หมดแล้วต้องจ่าย ไม่ใช่ฟรีถาวร |
| **Fly.io** | ⚠️ | ✅ | บัญชีใหม่ไม่มี free tier แบบเดิมแล้ว ต้องผูกบัตร |
| **โฮสต์บอท Discord ฟรีเฉพาะทาง** (เช่น bot-hosting.net ฯลฯ) | ⚠️ | ❌ | ส่วนใหญ่ใช้ระบบ "เหรียญ/ต่ออายุ" ทุกไม่กี่วัน, สเปกต่ำ, ความเสถียรไม่แน่นอน, ต้องฝาก token ไว้กับผู้ให้บริการ |
| **Replit / GitHub Codespaces / PythonAnywhere (ฟรี)** | ❌ | ❌ | ไม่รองรับการรันค้าง 24/7 ในแผนฟรี หรือบล็อกการเชื่อมต่อ Discord |

**คำแนะนำ:**
- อยากฟรีจริงและง่ายสุด → **คอมเก่า / Raspberry Pi** ที่บ้าน
- อยากได้คลาวด์ฟรีถาวรและรับการผูกบัตรยืนยันได้ → **Oracle Cloud Always Free**
- ไม่ว่าโฮสต์ที่ไหน: ใส่ token/API key ผ่าน **Environment Variables** ของบริการนั้น (ไม่ต้องอัปโหลดไฟล์ `.env`) — โค้ดนี้อ่านจาก environment variables ได้อยู่แล้ว

### ตัวอย่าง: รันค้างบน Linux VPS ด้วย systemd

```ini
# /etc/systemd/system/discord-ai-bot.service
[Unit]
Description=Discord AI Bot
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/Bot-Ai-Discord
ExecStart=/home/ubuntu/Bot-Ai-Discord/.venv/bin/python main.py
Restart=always
RestartSec=10
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-ai-bot
journalctl -u discord-ai-bot -f   # ดู log
```

`Restart=always` จะรีสตาร์ทบอทให้อัตโนมัติถ้าเครื่องรีบูตหรือโปรแกรมหยุด
