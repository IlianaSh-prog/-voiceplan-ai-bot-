# -*- coding: utf-8 -*-
"""Smoke-тест напоминаний: 15 мин / 1 час / 1 день, мультивыбор, календарь, миграция."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

tmp_db = os.path.join(tempfile.gettempdir(), "voiceplan_smoke_rem.db")
if os.path.exists(tmp_db):
    os.remove(tmp_db)
os.environ["DB_PATH"] = tmp_db
os.environ.setdefault("PORT", "18998")

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _sep, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

import bot  # noqa: E402

UID = 888888
failures = []


def check(name, condition):
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failures.append(name)


def db(sql, args=(), fetch=False):
    with bot.db_lock:
        conn = bot.get_db()
        try:
            cur = conn.execute(sql, args)
            result = cur.fetchall() if fetch else None
            conn.commit()
            return result
        finally:
            conn.close()


db("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (UID,))

check("choices include 15/30/60/1440", set(bot.REMINDER_CHOICES) == {"15", "30", "60", "1440"})
check("no 10 in choices", "10" not in bot.REMINDER_CHOICES)
check("quick buttons 15/30/60", list(bot.REMINDER_QUICK) == ["15", "30", "60"])

# 1. Мультивыбор: все три интервала.
db("UPDATE users SET reminder_offsets=? WHERE user_id=?", (json.dumps([1440, 60, 15]), UID))
_tz, offsets, _v = bot.get_user_preferences(UID)
check("three offsets stored", sorted(offsets) == [15, 60, 1440])
text = bot.format_reminder_labels(UID, offsets)
check("labels show all three", "15 минут" in text and "1 час" in text and "1 день" in text)
check("labels order ascending", text.index("15") < text.index("1 час") < text.index("1 день"))

# 2. Календарь: строка «Напоминания» в месяце.
now = datetime.now(timezone.utc) + timedelta(days=3)
event_id = bot.create_event(UID, "Встреча", now.astimezone().isoformat(timespec="seconds"),
                            "Europe/Moscow", [15, 60, 1440])
check("event created", bool(event_id))
rem = db("SELECT due_utc FROM reminders WHERE event_id=?", (event_id,), fetch=True)
check("3 reminders scheduled", len(rem) == 3)

local = now.astimezone(bot.ZoneInfo("Europe/Moscow"))
cal_text, _markup = bot.build_calendar(UID, local.year, local.month)
check("calendar has reminders line", "Напоминания:" in cal_text)
check("calendar line lists all three", "15 минут" in cal_text and "1 час" in cal_text and "1 день" in cal_text)

items = bot._events_by_local_date(UID).get(local.date(), [])
check("day items carry 3 fire times", bool(items) and len(items[0][2]) == 3)

# 3. Пустой выбор.
check("empty -> none selected", bot.format_reminder_labels(UID, []) == "не выбраны")

# 4. Миграция 10 -> 15 (идемпотентно).
UID2 = 777777
db("INSERT OR IGNORE INTO users (user_id, reminder_offsets) VALUES (?, ?)",
   (UID2, json.dumps([1440, 60, 10])))
start = datetime.now(timezone.utc) + timedelta(days=2)
cur_id = db("INSERT INTO events (user_id, title, start_utc, timezone, reminders_json) "
            "VALUES (?, 'Старое', ?, 'Europe/Moscow', ?)",
            (UID2, start.isoformat(timespec="seconds"), json.dumps([10, 60])))
ev = db("SELECT event_id FROM events WHERE user_id=?", (UID2,), fetch=True)[0]["event_id"]
old_due = (start - timedelta(minutes=10)).isoformat(timespec="seconds")
db("INSERT INTO reminders (event_id, user_id, due_utc, sent) VALUES (?, ?, ?, 0)",
   (ev, UID2, old_due))

for _ in range(2):  # повторный запуск не должен ничего ломать
    with bot.db_lock:
        conn = bot.get_db()
        try:
            bot.migrate_reminder_10_to_15(conn)
            conn.commit()
        finally:
            conn.close()

_tz, offs2, _v = bot.get_user_preferences(UID2)
check("user offsets migrated", sorted(offs2) == [15, 60, 1440])
ev_json = json.loads(db("SELECT reminders_json FROM events WHERE event_id=?", (ev,), fetch=True)[0][0])
check("event offsets migrated", sorted(ev_json) == [15, 60])
new_due = (start - timedelta(minutes=15)).isoformat(timespec="seconds")
due_rows = [r[0] for r in db("SELECT due_utc FROM reminders WHERE event_id=?", (ev,), fetch=True)]
check("pending reminder moved to -15 min", due_rows == [new_due])

# 5. Новая логика: событие без напоминания + произвольное время кнопками/текстом.
check("parse_custom_reminder 30 min", bot.parse_custom_reminder("за 30 минут") == 30)
check("parse_custom_reminder 2 hours", bot.parse_custom_reminder("за 2 часа") == 120)
check("parse_custom_reminder 1 day", bot.parse_custom_reminder("за 1 день") == 1440)
check("parse_custom_reminder garbage", bot.parse_custom_reminder("привет") is None)

# Бесплатный пользователь (баланс 0): событие создаётся БЕЗ напоминания и попадает в список выбора.
free_uid = 999999
db("INSERT OR IGNORE INTO users (user_id, balance_paid, balance_bonus) VALUES (?, 0, 0)", (free_uid,))
check("no paid balance", bot.has_paid_balance(free_uid) is False)
future = (datetime.now(bot.ZoneInfo("Europe/Moscow")) + timedelta(days=5)).replace(tzinfo=None)
data_free = {"new_events": [{"title": "Тест", "local_datetime": future.isoformat()}]}
counts_free = bot.save_ai_result(free_uid, data_free, use_voice_limit=False)
check("save_ai_result returns 4 values", len(counts_free) == 4)
check("free event created without reminder", counts_free[1] == 1 and len(counts_free[3]) == 1)
ev2_id, ev2_title = counts_free[3][0]
check("picker carries event id/title", isinstance(ev2_id, int) and ev2_title == "Тест")
rem2 = db("SELECT COUNT(*) c FROM reminders WHERE event_id=?", (ev2_id,), fetch=True)[0]["c"]
check("free event has no reminder row", rem2 == 0)

# Установка кнопкой «30 минут» создаёт одно напоминание.
check("set_event_reminder 30", bot.set_event_reminder(free_uid, ev2_id, 30) == 30)
rem3 = db("SELECT due_utc FROM reminders WHERE event_id=?", (ev2_id,), fetch=True)
check("one reminder row after button", len(rem3) == 1)

# Платный пользователь (баланс > 0): ИИ-время из remind_minutes применяется сразу.
paid_uid = 999998
db("INSERT OR IGNORE INTO users (user_id, balance_paid, balance_bonus) VALUES (?, 100, 0)", (paid_uid,))
check("has paid balance", bot.has_paid_balance(paid_uid) is True)
future2 = (datetime.now(bot.ZoneInfo("Europe/Moscow")) + timedelta(days=6)).replace(tzinfo=None)
data_paid = {"new_events": [{"title": "Оплата", "local_datetime": future2.isoformat(), "remind_minutes": 90}]}
counts_paid = bot.save_ai_result(paid_uid, data_paid, use_voice_limit=False)
check("paid event no picker needed", counts_paid[1] == 1 and len(counts_paid[3]) == 0)
ev3_id = db("SELECT event_id FROM events WHERE user_id=? ORDER BY event_id DESC LIMIT 1", (paid_uid,), fetch=True)[0]["event_id"]
rem4 = db("SELECT due_utc FROM reminders WHERE event_id=?", (ev3_id,), fetch=True)
check("paid event got 90-min reminder", len(rem4) == 1)

print()
print("SMOKE OK" if not failures else "FAILED: " + ", ".join(failures))
raise SystemExit(1 if failures else 0)
