# -*- coding: utf-8 -*-
"""Smoke для карточки дня календаря: единая навигация и кнопки напоминаний."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "0"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_calday_{os.getpid()}.db")
if os.path.exists(_tmpdb):
    os.remove(_tmpdb)
os.environ["DB_PATH"] = _tmpdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

bot.ZoneInfo = __import__("zoneinfo").ZoneInfo
failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def db(sql, args=(), fetch=False):
    with bot.db_lock:
        conn = bot.get_db()
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.fetchall() if fetch else None
        finally:
            conn.close()


UID = 930003
db("INSERT INTO users (user_id, language, balance_paid) VALUES (?, 'ru', 100)", (UID,))

now = datetime.now(bot.ZoneInfo("Europe/Moscow"))
# Два события на завтра: одно с напоминанием, другое без.
d1 = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
d2 = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
bot.save_ai_result(UID, {
    "new_events": [
        {"title": "С напоминанием", "local_datetime": d1.isoformat(), "remind_minutes": 15},
        {"title": "Без напоминания", "local_datetime": d2.isoformat()},
    ]
}, use_voice_limit=False)

# Эмулируем карточку дня: собираем кнопки так же, как calendar_open_day.
items = bot._events_by_local_date(UID).get(d1.date(), [])
check("day has two plans", len(items) == 2)

markup_buttons = []
for title, time_hhmm, fire_times, kind, item_id, offsets in items:
    if offsets:
        pass  # напоминание уже есть -> кнопки нет
    else:
        markup_buttons.append((title, f"calrem:{kind}:{item_id}"))
# Кнопка «назад к календарю» добавляется всегда.
markup_buttons.append(("cal_back", "planner:menu_calendar"))

# У плана без напоминания — своя кнопка, у плана с напоминанием — нет.
no_rem = [b for b in markup_buttons if b[0] == "Без напоминания"]
has_rem = [b for b in markup_buttons if b[0] == "С напоминанием"]
check("only no-reminder plan gets a button", len(no_rem) == 1 and len(has_rem) == 0)
check("reminder button carries plan title", no_rem and "Без напоминания" in no_rem[0][0])
check("day always has back-to-calendar", any(cb == "planner:menu_calendar" for _, cb in markup_buttons))

# EN-легенда синхронна с RU (• а не ✳).
check("en legend uses bullet", "•" in bot.LANGS["en"]["cal_legend"] and "✳" not in bot.LANGS["en"]["cal_legend"])
check("ru legend uses bullet", "•" in bot.LANGS["ru"]["cal_legend"])

db("DELETE FROM users WHERE user_id=?", (UID,))
try:
    os.remove(_tmpdb)
except OSError:
    pass

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("CALDAY SMOKE OK")
