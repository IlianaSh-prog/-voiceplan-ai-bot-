# -*- coding: utf-8 -*-
"""Smoke для карточки дня календаря: навигация, напоминания, удаление."""
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

# Карточка дня собирается реальным хелпером.
items = bot._events_by_local_date(UID).get(d1.date(), [])
check("day has two plans", len(items) == 2)

text, markup = bot._build_day_card(UID, d1.date())
rows = [(b.text, b.callback_data) for r in markup.keyboard for b in r]
cbs = [cb for _, cb in rows]

# У плана без напоминания — кнопка ⏰, у плана с напоминанием — «Изменить».
calrem_rows = [(t, cb) for t, cb in rows if cb.startswith("calrem:")]
check("every plan gets a reminder button", len(calrem_rows) == 2)
check("reminder button carries date+kind+id",
      all(cb.split(":")[1] == d1.date().isoformat() and cb.split(":")[2] == "event"
          for _t, cb in calrem_rows))
check("no-reminder plan button has ⏰ and title",
      any("Без напоминания" in t and t.startswith("⏰") for t, _cb in calrem_rows))
check("reminder plan button offers change",
      any("С напоминанием" in t and "Изменить" in t for t, _cb in calrem_rows))

# Удаление: у каждого плана своя кнопка с подтверждением (caldelask).
del_btns = [cb for cb in cbs if cb.startswith("caldelask:")]
check("every plan gets a delete button", len(del_btns) == 2)
check("delete callback carries date+kind",
      all(cb.split(":")[1] == d1.date().isoformat() and cb.split(":")[2] == "event"
          for cb in del_btns))
check("day always has back-to-calendar", "planner:menu_calendar" in cbs)

# Подтверждение удаления: заголовок/тело есть в RU и EN.
for lang in ("ru", "en"):
    for key in ("cal_delete_btn", "cal_delete_confirm_title",
                "cal_delete_confirm_body", "task_delete_yes", "task_delete_no"):
        check(f"{lang} key present: {key}", key in bot.LANGS[lang])
check("confirm body accepts title",
      "{title}" in bot.LANGS["ru"]["cal_delete_confirm_body"]
      and "{title}" in bot.LANGS["en"]["cal_delete_confirm_body"])

# Удаляем событие БЕЗ напоминания (items[1]), напоминание снимаем с items[0].
ev_rem_id = items[0][4]  # событие с напоминанием
ev_id = items[1][4]      # событие без напоминания
check("_delete_plan removes event", bot._delete_plan(UID, "event", ev_id))
left = [i for i in bot._events_by_local_date(UID).get(d1.date(), []) if i[4] == ev_id]
check("deleted event gone from day", not left)

# Снятие напоминания у живого события.
check("_clear_plan_reminder works", bot._clear_plan_reminder(UID, "event", ev_rem_id))
after = next(i for i in bot._events_by_local_date(UID).get(d1.date(), []) if i[4] == ev_rem_id)
check("reminder cleared -> offsets empty", after[5] == [])

# Список ближайших событий: кнопки ⏰/🔄 + навигация в одну строку.
ev_text, ev_markup = bot.upcoming_events_text(UID)
ev_rows = [(b.text, b.callback_data) for r in ev_markup.keyboard for b in r]
ev_cbs = [cb for _t, cb in ev_rows]
check("events list has evtpick button", any(cb.startswith("evtpick:") for cb in ev_cbs))
check("events list has eventdel button", any(cb.startswith("eventdel:") for cb in ev_cbs))
nav_row = [cb for cb in ev_cbs if cb in ("planner:menu_calendar", "planner:goto_folders")]
check("events list nav is compact row", nav_row == ["planner:menu_calendar", "planner:goto_folders"])

# Клавиатура подтверждения удаления дня и пикера использует новые ключи.
for key in ("cal_remind_change_btn", "cal_remind_off_btn", "cal_reminder_current", "evt_back"):
    check(f"ru+en key: {key}", key in bot.LANGS["ru"] and key in bot.LANGS["en"])

# Отмена удаления: карточка дня собирается без ошибок и остаётся с второй кнопкой 🗑.
text2, markup2 = bot._build_day_card(UID, d1.date())
cbs2 = [b.callback_data for r in markup2.keyboard for b in r]
check("day still has one delete button after delete",
      sum(1 for cb in cbs2 if cb.startswith("caldelask:")) == 1)

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
