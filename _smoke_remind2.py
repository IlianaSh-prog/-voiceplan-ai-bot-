# -*- coding: utf-8 -*-
"""Тест выбора времени напоминания: без автоприсвоения, кнопки, своё время."""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TMP = tempfile.mkdtemp(prefix="vp_remind_")
DB = os.path.join(TMP, "test.db")
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "0:test",
    "ADMIN_ID": "1",
    "DATABASE_PATH": DB,
    "AI_API_KEY": "k",
    "VOICEPLAN_USE_PROXY": "0",
})

import bot  # noqa: E402

bot.DB_PATH = DB
bot.init_db()

UID_FREE = 424242
UID_PAID = 424243
future = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)).replace(tzinfo=None, second=0, microsecond=0)

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def db(sql, args=(), one=False):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def events_of(uid):
    rows = db("SELECT * FROM events WHERE user_id=?", (uid,))
    return [(r["reminders_json"], r["start_utc"]) for r in rows]


def picker_for(uid, event_id, title="Встреча"):
    sent = []
    bot.bot.send_message = lambda chat_id, text, **kw: sent.append(
        (text, getattr(kw.get("reply_markup"), "keyboard", None), kw.get("reply_markup"))
    )
    bot.send_reminder_picker(chat_id=1, uid=uid, event_id=event_id, title=title)
    return sent


# 1. Бесплатный + голос с «напомни за 45 минут» → напоминание НЕ ставится,
#    возвращается список для выбора кнопками.
db("INSERT OR IGNORE INTO users (user_id, balance_paid, balance_bonus) VALUES (?, 0, 0)", (UID_FREE,))
data = {"new_events": [{"title": "Встреча", "local_datetime": future.isoformat(),
                        "remind_minutes": 45}]}
res = bot.save_ai_result(UID_FREE, data, use_voice_limit=True)
check("save_ai_result returns 4 items", len(res) == 4)
check("event saved", res[1] == 1)
check("no reminder auto-set for free", res[3] and res[3][0][1] == "Встреча")
check("reminders_json empty for free", events_of(UID_FREE)[0][0] == "[]")

# 2. Платный + «за 45 минут» → напоминание ставится сразу.
db("INSERT OR IGNORE INTO users (user_id, balance_paid, balance_bonus) VALUES (?, 100, 0)", (UID_PAID,))
res2 = bot.save_ai_result(UID_PAID, data, use_voice_limit=False)
check("paid: reminder auto-set", not res2[3])
check("paid: reminders_json == [45]", json.loads(events_of(UID_PAID)[0][0]) == [45])

# 3. Платный без указания времени → кнопки.
data_no = {"new_events": [{"title": "Созвон", "local_datetime": (future + timedelta(days=1)).isoformat()}]}
res3 = bot.save_ai_result(UID_PAID, data_no)
check("paid without remind -> picker list", len(res3[3]) == 1)

# 4. Платный + невалидное remind_minutes (0 / 99999) → кнопки.
data_bad = {"new_events": [{"title": "X", "local_datetime": (future + timedelta(days=2)).isoformat(),
                            "remind_minutes": 99999}]}
res4 = bot.save_ai_result(UID_PAID, data_bad)
check("paid + out-of-range remind -> picker", len(res4[3]) == 1)

# 5. Клавиатура выбора: 15/30/60 + своё время + без напоминания.
eid = res[3][0][0]
sent = picker_for(UID_FREE, eid)
text, _, markup = sent[0]
cbs = [b.callback_data for row in markup.keyboard for b in row]
check("picker has 15 min", f"evtrem:{eid}:15" in cbs)
check("picker has 30 min", f"evtrem:{eid}:30" in cbs)
check("picker has 1 hour", f"evtrem:{eid}:60" in cbs)
check("picker has custom", f"evtremc:{eid}" in cbs)
check("picker has skip", f"evtrems:{eid}" in cbs)
check("picker text mentions reminder", "напомин" in text.lower())

# 6. Нажатие кнопки 30 минут → reminders_json == [30].
bot.set_event_reminder(UID_FREE, int(eid), 30)
check("button 30 sets reminder", json.loads(events_of(UID_FREE)[0][0]) == [30])

# 7. Парсер своего времени.
check("parse 'за 30 минут'", bot.parse_custom_reminder("за 30 минут") == 30)
check("parse 'через 2 часа'", bot.parse_custom_reminder("через 2 часа") == 120)
check("parse '1 день'", bot.parse_custom_reminder("1 день") == 1440)
check("parse junk -> None", bot.parse_custom_reminder("привет") is None)

# 8. Кнопка «своё время» ставит состояние.
bot.bot.answer_callback_query = lambda *a, **k: None
bot.bot.send_message = lambda *a, **k: sent.append(a)
call = type("C", (), {"data": f"evtremc:{eid}", "id": "cb1",
                      "from_user": type("U", (), {"id": UID_FREE})(),
                      "message": type("M", (), {"chat": type("CH", (), {"id": 1})()})()})()
bot.event_reminder_custom(call)
check("custom state set", bot.user_states.get(UID_FREE) == f"custom_reminder:{eid}")

# 9. Миграция: 10 -> 15 по-прежнему работает.
db("UPDATE users SET reminder_offsets=? WHERE user_id=?", (json.dumps([10, 60]), UID_FREE))
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
try:
    bot.migrate_reminder_10_to_15(conn)
    conn.commit()
finally:
    conn.close()
_tz, offs, _v = bot.get_user_preferences(UID_FREE)
check("migration 10 -> 15", sorted(offs) == [15, 60])

print()
if failures:
    print("FAILURES: " + ", ".join(failures))
    sys.exit(1)
print("SMOKE-REMIND OK")
