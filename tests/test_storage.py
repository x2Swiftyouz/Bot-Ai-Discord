from bot.memory import ChannelMemory
from bot.storage import Database


def test_memory_survives_restart_and_trims(tmp_path):
    db = Database(tmp_path / "bot.db")
    memory = ChannelMemory(4, db)
    for i in range(3):
        memory.add_exchange(1, f"u{i}", f"a{i}")
    db.close()

    db = Database(tmp_path / "bot.db")
    memory = ChannelMemory(4, db)
    assert [m.content for m in memory.get(1)] == ["u1", "a1", "u2", "a2"]
    assert memory.remove_exchange(1, "u2", "a2")
    assert [m.content for m in memory.get(1)] == ["u1", "a1"]
    memory.reset(1)
    assert memory.get(1) == []
    db.close()


def test_usage_quota_and_notes(tmp_path):
    db = Database(tmp_path / "bot.db")
    for ok in (True, True, False):
        db.record_usage(day="2026-10-04", guild_id=9, user_id=7, user_name="p", ok=ok, model="m")
    assert db.used_today("2026-10-04", 7) == 2  # error ไม่นับ
    assert db.day_stats("2026-10-04", 9).errors == 1

    db.add_note(7, "ชอบเล่นเกม")
    db.add_note(7, "ชื่อปีเตอร์")
    assert db.delete_note(7, 1) == "ชอบเล่นเกม"
    assert db.notes(7) == ["ชื่อปีเตอร์"]
    assert db.delete_note(7, 9) is None

    db.set_setting("persona:1", "cat")
    assert db.get_setting("persona:1") == "cat"
    db.close()
