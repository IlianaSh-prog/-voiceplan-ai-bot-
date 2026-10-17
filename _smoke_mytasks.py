# -*- coding: utf-8 -*-
"""Smoke «Мои задачи»: выбор месяца → список дат с делами и напоминаниями."""
import json
import os
import re
import sys
import tempfile
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "0"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_mytasks_{os.getpid()}.db")
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


TZ = "Europe/Moscow"
UID = 950005
UID2 = 950006
db("INSERT INTO users (user_id, language, balance_paid) VALUES (?, 'ru', 100)", (UID,))
db("INSERT INTO users (user_id, language, balance_paid) VALUES (?, 'ru', 100)", (UID2,))

tz = bot.ZoneInfo(TZ)
now_local = datetime.now(tz)
# Фиксированные дни текущего месяца: 5-е и 20-е всегда попадают в один месяц.
day1 = now_local.replace(day=5, hour=10, minute=0, second=0, microsecond=0)
day2 = now_local.replace(day=20, hour=9, minute=0, second=0, microsecond=0)
year, month = day1.year, day1.month


def add_event(title, local_dt, offsets):
    start_utc = local_dt.astimezone(bot.timezone.utc).isoformat(timespec="seconds")
    db(
        "INSERT INTO events (user_id, title, start_utc, timezone, reminders_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (UID, title, start_utc, TZ, json.dumps(offsets)),
    )


add_event("Встреча с клиентом", day1, [15])
add_event("Сдать отчёт", day1.replace(hour=12), [])
add_event("День рождения", day2, [])
db(
    "INSERT INTO tasks (user_id, category, task_text, due_utc, remind_before_min, timezone) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (UID, "Работа", "Позвонить партнёру",
     day1.replace(hour=14).astimezone(bot.timezone.utc).isoformat(timespec="seconds"),
     60, TZ),
)

# 1. Доступ календаря: с положительным балансом открыт.
check("paid access open", bot.calendar_access(UID)[0] is True)

# 2. Пикер: 12 месяцев, годы, кнопки навигации, счётчик у месяца с планами.
text, markup = bot._my_tasks_picker(UID, year)
rows = [(b.text, b.callback_data) for r in markup.keyboard for b in r]
cbs = [cb for _t, cb in rows]
month_cbs = [cb for cb in cbs if cb.startswith("mytasks:mo:")]
check("picker has 12 months", len(month_cbs) == 12)
check("picker month callbacks well-formed",
      all(re.fullmatch(r"mytasks:mo:\d{4}-(?:[1-9]|1[0-2])", cb) for cb in month_cbs))
check("picker year nav", f"mytasks:yr:{year - 1}" in cbs and f"mytasks:yr:{year + 1}" in cbs)
check("picker keeps folders+calendar+menu",
      "planner:goto_folders" in cbs and "planner:menu_calendar" in cbs
      and "planner:menu" in cbs)
check("planned month marked green with count",
      any(cb == f"mytasks:mo:{year}-{month}" and t.startswith("🟢") and "· 4" in t
          for t, cb in rows))

# 3. Пустой пикер — подсказка про календарь.
text2, _mk = bot._my_tasks_picker(UID2, year)
check("picker empty hint", bot.LANGS["ru"]["mt_empty_hint"] in text2)

# 4. Месяц: счётчик, разделители дат с днём недели, даты по порядку.
t, mk = bot._my_tasks_month_view(UID, year, month)
lines = t.split("\n")
check("month title counts entries", "записей: 4" in t)
headers = [ln for ln in lines if re.fullmatch(r"━━ \d{2}\.\d{2} · \S+ ━━", ln)]
check("two day headers", len(headers) == 2)
exp_hdr1 = bot.LANGS["ru"]["mt_day_header"].format(
    date=day1.strftime("%d.%m"), weekday=bot.WEEKDAYS_FULL["ru"][day1.weekday()])
check("header carries real weekday", exp_hdr1 in t)
pos1, pos2 = t.find(day1.strftime("%d.%m")), t.find(day2.strftime("%d.%m"))
check("dates ascending", 0 <= pos1 < pos2)

# 5. Напоминание — строкой под своим делом; у дела без него ⏰-строки нет.
def idx(fragment):
    return next(i for i, ln in enumerate(lines) if fragment in ln)

i_a = idx("Встреча с клиентом")
check("event reminder directly under event",
      lines[i_a + 1].startswith("   ⏰") and bot._offset_label(UID, 15) in lines[i_a + 1])
i_b = idx("Сдать отчёт")
check("no reminder -> no ⏰ line under it", not lines[i_b + 1].startswith("   ⏰"))
i_t = idx("Позвонить партнёру")
check("task marked with done icon", lines[i_t].startswith("✅"))
check("task reminder directly under task",
      lines[i_t + 1].startswith("   ⏰") and bot._offset_label(UID, 60) in lines[i_t + 1])

# 6. Кнопки: даты → карточка дня, соседние месяцы, назад к месяцам.
cbs_m = [b.callback_data for r in mk.keyboard for b in r]
check("date buttons open day card",
      f"calday:{day1.date().isoformat()}" in cbs_m
      and f"calday:{day2.date().isoformat()}" in cbs_m)
idx_m = year * 12 + (month - 1)
py, pm = divmod(idx_m - 1, 12)
ny, nm = divmod(idx_m + 1, 12)
check("prev/next month buttons",
      f"mytasks:mo:{py}-{pm + 1}" in cbs_m and f"mytasks:mo:{ny}-{nm + 1}" in cbs_m)
check("back to months picker", "mytasks:menu" in cbs_m)
check("month text fits telegram limit", len(t) < 4096)

# 7. Пустой месяц: явное сообщение и нулевой счётчик.
em_month = month + 1 if month < 12 else 1
em_year = year if month < 12 else year + 1
t3, _mk3 = bot._my_tasks_month_view(UID, em_year, em_month)
check("empty month message", bot.LANGS["ru"]["mt_empty_month"] in t3)
check("empty month count 0", "записей: 0" in t3)

# 8. Пагинация: уменьшаем лимит — дни разъезжаются по страницам.
old_limit = bot._MT_PAGE_LIMIT
bot._MT_PAGE_LIMIT = 100
t4, mk4 = bot._my_tasks_month_view(UID, year, month, 0)
cbs4 = [b.callback_data for r in mk4.keyboard for b in r]
check("page indicator on long month",
      bot.LANGS["ru"]["mt_page_of"].format(page=1, pages=2) in t4)
check("next page button", f"mytasks:mo:{year}-{month}:1" in cbs4)
check("page 1 keeps first day only",
      day1.strftime("%d.%m") in t4 and day2.strftime("%d.%m") not in t4)
t5, _mk5 = bot._my_tasks_month_view(UID, year, month, 1)
check("page 2 has second day", day2.strftime("%d.%m") in t5)
t6, _mk6 = bot._my_tasks_month_view(UID, year, month, 99)
check("page out of range clamped",
      bot.LANGS["ru"]["mt_page_of"].format(page=2, pages=2) in t6)
bot._MT_PAGE_LIMIT = old_limit

# 9. i18n: новые ключи есть в RU и EN, плейсхолдеры совпадают.
mt_keys = ["mt_menu_title", "mt_month_title", "mt_day_header", "mt_empty_month",
           "mt_empty_hint", "mt_page_of", "mt_prev_page", "mt_next_page", "mt_back_months"]
ph = re.compile(r"{(\w+)}")
for key in mt_keys:
    in_both = key in bot.LANGS["ru"] and key in bot.LANGS["en"]
    check(f"ru+en key: {key}", in_both)
    if in_both:
        check(f"placeholders match: {key}",
              set(ph.findall(bot.LANGS["ru"][key])) == set(ph.findall(bot.LANGS["en"][key])))
check("weekday arrays cover week",
      len(bot.WEEKDAYS_FULL["ru"]) == 7 and len(bot.WEEKDAYS_FULL["en"]) == 7)

db("DELETE FROM users WHERE user_id IN (?, ?)", (UID, UID2))
try:
    os.remove(_tmpdb)
except OSError:
    pass

print()
if failures:
    print("FAILED: " + ", ".join(failures))
    sys.exit(1)
print("MYTASKS SMOKE OK")