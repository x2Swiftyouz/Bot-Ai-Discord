"""ฐานข้อมูล SQLite ของบอท (ไฟล์เดียว อยู่ใน DATA_DIR)

- messages : ความจำบทสนทนาแยกตามช่อง (ไม่หายเมื่อรีสตาร์ท)
- usage    : บันทึกการใช้งานแต่ละครั้ง ใช้นับโควต้าต่อคนและทำสถิติ /stats
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages (channel_id, id);

CREATE TABLE IF NOT EXISTS usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    day       TEXT    NOT NULL,  -- วันที่ตามเขตเวลาที่ตั้งไว้ (YYYY-MM-DD)
    guild_id  INTEGER,
    user_id   INTEGER NOT NULL,
    user_name TEXT    NOT NULL,
    ok        INTEGER NOT NULL,  -- 1 = ตอบสำเร็จ, 0 = error
    model     TEXT,
    backup    INTEGER NOT NULL DEFAULT 0,
    searched  INTEGER NOT NULL DEFAULT 0,
    elapsed   REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_day_user ON usage (day, user_id);

CREATE TABLE IF NOT EXISTS user_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    note       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_notes_user ON user_notes (user_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class DayStats:
    answers: int
    errors: int
    users: int
    backup: int
    searched: int
    avg_elapsed: float


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # บอททำงานใน thread เดียว (asyncio) และแต่ละคำสั่งใช้เวลาเป็นมิลลิวินาที จึงเรียกตรง ๆ ได้
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        log.info("Database: %s", path)

    def close(self) -> None:
        self.conn.close()

    # ---------- settings (ค่าที่ตั้งผ่านคำสั่งใน Discord เช่น ห้อง log, บุคลิกของห้อง) ----------

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def delete_setting(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        self.conn.commit()

    # ---------- user notes (/remember: ข้อมูลส่วนตัวที่ผู้ใช้ขอให้บอทจำ ใช้ได้ทุกห้อง) ----------

    def add_note(self, user_id: int, note: str) -> None:
        self.conn.execute(
            "INSERT INTO user_notes (user_id, note, created_at) VALUES (?, ?, ?)",
            (user_id, note, time.time()),
        )
        self.conn.commit()

    def notes(self, user_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT note FROM user_notes WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def delete_note(self, user_id: int, index: int) -> str | None:
        """ลบข้อที่ index (เริ่มที่ 1) คืนข้อความที่ลบ หรือ None ถ้าไม่มี"""
        rows = self.conn.execute(
            "SELECT id, note FROM user_notes WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
        if not 1 <= index <= len(rows):
            return None
        row_id, note = rows[index - 1]
        self.conn.execute("DELETE FROM user_notes WHERE id = ?", (row_id,))
        self.conn.commit()
        return note

    def clear_notes(self, user_id: int) -> int:
        cur = self.conn.execute("DELETE FROM user_notes WHERE user_id = ?", (user_id,))
        self.conn.commit()
        return cur.rowcount

    # ---------- usage ----------

    def record_usage(
        self,
        *,
        day: str,
        guild_id: int | None,
        user_id: int,
        user_name: str,
        ok: bool,
        model: str | None = None,
        backup: bool = False,
        searched: bool = False,
        elapsed: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO usage (ts, day, guild_id, user_id, user_name, ok, model, backup, searched,"
            " elapsed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), day, guild_id, user_id, user_name, int(ok), model, int(backup),
             int(searched), elapsed),
        )
        self.conn.commit()

    def used_today(self, day: str, user_id: int) -> int:
        """จำนวนคำตอบที่สำเร็จของผู้ใช้ในวันนั้น (error ไม่นับ)"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM usage WHERE day = ? AND user_id = ? AND ok = 1", (day, user_id)
        ).fetchone()
        return int(row[0])

    def day_stats(self, day: str, guild_id: int | None) -> DayStats:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(ok), 0), COALESCE(SUM(1 - ok), 0), COUNT(DISTINCT user_id),"
            " COALESCE(SUM(backup), 0), COALESCE(SUM(searched), 0),"
            " COALESCE(AVG(CASE WHEN ok = 1 THEN elapsed END), 0)"
            " FROM usage WHERE day = ? AND (? IS NULL OR guild_id = ?)",
            (day, guild_id, guild_id),
        ).fetchone()
        return DayStats(int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]),
                        float(row[5]))

    def top_users(self, day: str, guild_id: int | None, limit: int = 5) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT user_id, MAX(user_name), COUNT(*) AS n FROM usage"
            " WHERE day = ? AND ok = 1 AND (? IS NULL OR guild_id = ?)"
            " GROUP BY user_id ORDER BY n DESC LIMIT ?",
            (day, guild_id, guild_id, limit),
        ).fetchall()
        return [(name, int(n)) for _, name, n in rows]

    def models_used(self, day: str, guild_id: int | None) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT model, COUNT(*) AS n FROM usage"
            " WHERE day = ? AND ok = 1 AND model IS NOT NULL AND (? IS NULL OR guild_id = ?)"
            " GROUP BY model ORDER BY n DESC",
            (day, guild_id, guild_id),
        ).fetchall()
        return [(m, int(n)) for m, n in rows]

    def daily_totals(self, days: list[str], guild_id: int | None) -> list[tuple[str, int]]:
        placeholders = ",".join("?" * len(days))
        rows = dict(self.conn.execute(
            f"SELECT day, SUM(ok) FROM usage WHERE day IN ({placeholders})"
            " AND (? IS NULL OR guild_id = ?) GROUP BY day",
            (*days, guild_id, guild_id),
        ).fetchall())
        return [(d, int(rows.get(d) or 0)) for d in days]
