# -*- coding: utf-8 -*-
"""Smoke Планировщика Pro: продление и доступ к календарю без ИИ-баланса."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "0"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_planner_{os.getpid()}.db")
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


UID = 940004
db("INSERT INTO users (user_id, language, balance_paid, balance_bonus) VALUES (?, 'ru', 0, 0)", (UID,))

# 1. Без баланса и без Планировщика — календарь закрыт.
ok, reason = bot.calendar_access(UID)
check("no balance no planner -> expired", ok is False and reason == "expired")

# 2. Покупка Планировщика Pro на 30 дней.
bot._extend_planner(UID, bot.PLANNER_DAYS)
ok2, reason2 = bot.calendar_access(UID)
check("planner active -> ok", ok2 is True and reason2 == "planner")
check("planner_active True", bot.planner_active(UID) is True)

# 3. planner_until примерно +30 дней.
until = bot.planner_until(UID)
delta_days = (until - datetime.now(bot.timezone.utc)).total_seconds() / 86400
check("planner ~30 days", 29 <= delta_days <= 31)

# 4. Продление накапливается (докупаем ещё 30).
bot._extend_planner(UID, bot.PLANNER_DAYS)
until2 = bot.planner_until(UID)
delta2 = (until2 - datetime.now(bot.timezone.utc)).total_seconds() / 86400
check("planner stacks to ~60 days", 58 <= delta2 <= 62)

# 5. Просроченный Планировщик не считается.
db("UPDATE users SET planner_until=? WHERE user_id=?",
   ((datetime.now(bot.timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"), UID))
check("expired planner inactive", bot.planner_active(UID) is False)
ok3, reason3 = bot.calendar_access(UID)
check("expired planner -> expired", ok3 is False)

# 6. ИИ-баланс тоже открывает календарь (приоритет 'paid').
db("UPDATE users SET balance_paid=50 WHERE user_id=?", (UID,))
ok4, reason4 = bot.calendar_access(UID)
check("paid balance -> ok (paid)", ok4 is True and reason4 == "paid")

# 7. Цены-константы Планировщика на месте.
check("planner price/days constants", bot.PLANNER_PRICE_RUB == 99 and bot.PLANNER_DAYS == 30)

# 8. Тексты согласованы: оферта и «как работает» упоминают Планировщик.
check("oferta mentions Planner Pro", "Планировщик Pro" in bot.LANGS["ru"]["oferta_text"])
check("how mentions two products", "два продукта" in bot.LANGS["ru"]["how_balance_text"])

db("DELETE FROM users WHERE user_id=?", (UID,))
try:
    os.remove(_tmpdb)
except OSError:
    pass

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("PLANNER SMOKE OK")
