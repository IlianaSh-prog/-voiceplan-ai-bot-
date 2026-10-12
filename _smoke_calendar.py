# -*- coding: utf-8 -*-
# Smoke для новой архитектуры календаря: сетка, день, напоминания из дня,
# выбранная дата в ИИ, гейт доступа (триал/баланс). Изолированная БД.
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "0"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_cal_{os.getpid()}.db")
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


UID = 910001
db("INSERT INTO users (user_id, language) VALUES (?, 'ru')", (UID,))

# 1. Сетка календаря: каждый день — кликабельная кнопка, дни с планами «•».
now = datetime.now(bot.ZoneInfo("Europe/Moscow"))
cal_text, markup = bot.build_calendar(UID, now.year, now.month)
check("grid has day buttons", any(
    (getattr(b, "callback_data", "") or "").startswith("calday:")
    for row in markup.keyboard for b in row
))
check("grid has year nav", any(
    (getattr(b, "callback_data", "") or "").startswith("calnav:")
    for row in markup.keyboard for b in row
))
check("grid shows tap hint", "Нажмите" in cal_text or "дату" in cal_text)

# 2. Событие на завтра + напоминание за 15 минут.
tomorrow = (now + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
data = {"new_events": [{"title": "Созвон", "local_datetime": tomorrow.isoformat(),
                        "remind_minutes": 15}]}
db("UPDATE users SET balance_paid=100 WHERE user_id=?", (UID,))
counts = bot.save_ai_result(UID, data, use_voice_limit=False)
check("event saved with reminder", counts[1] == 1 and len(counts[3]) == 0)

# 3. День календаря показывает событие и ⏰ за 15 минут.
items = bot._events_by_local_date(UID).get(tomorrow.date(), [])
check("day has the event", bool(items))
check("day offsets carry 15", bool(items) and 15 in items[0][5])
check("offset label 15 min", bot._offset_label(UID, 15) == "за 15 минут")

# 4. Событие без напоминания -> есть кнопка «Поставить напоминание».
day2 = (now + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
bot.create_event(UID, "Без напоминания", day2.isoformat(), "Europe/Moscow", [])
items2 = bot._events_by_local_date(UID).get(day2.date(), [])
check("day2 has event without reminder", bool(items2) and not items2[0][5])

# 5. _set_plan_reminder по событию и по задаче.
ev2_id = items2[0][4]
check("set reminder on event", bot._set_plan_reminder(UID, "event", ev2_id, 60))
rows = db("SELECT remind_before_min FROM tasks WHERE user_id=? AND due_utc IS NOT NULL",
          (UID,), fetch=True)
# Задача с датой для проверки task-ветки.
db("INSERT INTO tasks (user_id, category, task_text, due_utc, remind_before_min, "
   "remind_utc, remind_sent, timezone) VALUES (?, '📋 Повседневное', 'Задача', ?, NULL, "
   "NULL, 0, 'Europe/Moscow')",
   (UID, (now + timedelta(days=3)).replace(tzinfo=None).isoformat()))
tid = db("SELECT task_id FROM tasks WHERE user_id=? ORDER BY task_id DESC LIMIT 1",
         (UID,), fetch=True)[0]["task_id"]
check("set reminder on task", bot._set_plan_reminder(UID, "task", tid, 30))

# 6. force_date: наговорили задачу без даты при выбранной дате календаря.
picked = now.date() + timedelta(days=4)
res = bot.save_ai_result(
    UID, {"new_tasks": [{"text": "Купить хлеб", "category": "🛒 Покупки"}]},
    use_voice_limit=False, force_date=picked,
)
check("forced-date task inserted", res[0] >= 1)
trow = db("SELECT due_utc FROM tasks WHERE user_id=? AND task_text='Купить хлеб'",
          (UID,), fetch=True)
check("forced due date matches picked", bool(trow) and trow[0]["due_utc"][:10] == picked.isoformat())

# 7. Гейт доступа: нулевой баланс, триал не начат -> закрыт.
FREE = 920002
db("INSERT INTO users (user_id, language) VALUES (?, 'ru')", (FREE,))
ok, reason = bot.calendar_access(FREE)
check("no balance no trial -> expired", ok is False and reason == "expired")
# Запускаем триал -> доступен.
db("UPDATE users SET trial_started_at=? WHERE user_id=?",
   (datetime.now(bot.__dict__.get("timezone").utc).isoformat(timespec="seconds"), FREE))
ok2, reason2 = bot.calendar_access(FREE)
check("trial active -> ok", ok2 is True and reason2 == "trial")
check("trial days left > 0", bot.calendar_trial_days_left(FREE) > 0)
# Платёжный баланс открывает даже после триала.
db("UPDATE users SET balance_paid=50 WHERE user_id=?", (UID,))
ok3, reason3 = bot.calendar_access(UID)
check("paid balance -> ok", ok3 is True and reason3 == "paid")

# 8. Приветствие о 7 днях показывается один раз.
db("UPDATE users SET calendar_free_ack=0 WHERE user_id=?", (FREE,))
sent = {"n": 0}
_orig_send = bot.bot.send_message
def _fake_send(chat_id, text, *a, **k):
    sent["n"] += 1
    return None
bot.bot.send_message = _fake_send
try:
    bot._maybe_show_trial_welcome(FREE, FREE)
    first = sent["n"]
    bot._maybe_show_trial_welcome(FREE, FREE)
    second = sent["n"]
finally:
    bot.bot.send_message = _orig_send
check("trial welcome shown once", first == 1 and second == 1)
check("trial welcome text is professional", "7" in bot.LANGS["ru"]["cal_trial_welcome"])

db("DELETE FROM users WHERE user_id IN (?, ?)", (UID, FREE))
try:
    os.remove(_tmpdb)
except OSError:
    pass

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("CALENDAR SMOKE OK")
