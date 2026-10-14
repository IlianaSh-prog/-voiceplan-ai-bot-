# -*- coding: utf-8 -*-
"""Smoke квоты голосовых задач: 5/день бесплатно, далее с баланса, затем отказ."""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "0"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_voice_{os.getpid()}.db")
if os.path.exists(_tmpdb):
    os.remove(_tmpdb)
os.environ["DB_PATH"] = _tmpdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

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


# 1. Новые цены на месте.
check("image price 9", bot.PRICE_IMAGE_RUB == 9)
check("tts price 3", bot.PRICE_TTS_RUB_PER_1K == 3)
check("image quality low", bot.IMAGE_QUALITY == "low")
check("voice quota 5/day", bot.FREE_VOICE_TASKS_PER_DAY == 5)
check("extra voice 2 rub", bot.VOICE_TASK_PRICE_RUB == 2)

# 2. Пользователь с нулевым балансом: 5 дневных, далее отказ.
UID = 950005
db("INSERT INTO users (user_id, language, balance_paid, balance_bonus) VALUES (?, 'ru', 0, 0)", (UID,))
reasons = [bot.try_consume_voice_task(UID)[1] for _ in range(6)]
check("first 5 from daily quota", reasons[:5] == ["daily"] * 5)
check("6th blocked (no balance)", reasons[5] == "blocked")
check("daily_voice_left is 0", bot.get_voice_quota(UID)[0] == 0)

# 3. С балансом: сверх квоты списывается 2₽.
db("UPDATE users SET balance_paid=10, daily_voice_left=0 WHERE user_id=?", (UID,))
ok, reason = bot.try_consume_voice_task(UID)
check("over-quota allowed with balance", ok is True and reason == "paid")
paid, bonus, *_ = bot.get_balance(UID)
check("2 rub charged", round(paid + bonus, 2) == 8.0)

# 4. Premium (легаси) — безлимит.
UID2 = 950006
db("INSERT INTO users (user_id, language, is_premium, balance_paid) VALUES (?, 'ru', 1, 0)", (UID2,))
ok2, reason2 = bot.try_consume_voice_task(UID2)
check("premium unlimited voice", ok2 is True and reason2 == "premium")

# 5. Экономика Планировщика Pro: 5 голос/день себестоимость < цены.
voice_cost_per_month = 5 * 30 * 0.55  # ~Whisper
check("planner voice cost < price", voice_cost_per_month < bot.PLANNER_PRICE_RUB)

# 6. Тексты на месте.
check("voice_tasks_limit RU", "5 голосовых" in bot.LANGS["ru"]["voice_tasks_limit"])
check("voice_tasks_limit EN", "5 voice tasks" in bot.LANGS["en"]["voice_tasks_limit"])

db("DELETE FROM users WHERE user_id IN (?, ?)", (UID, UID2))
try:
    os.remove(_tmpdb)
except OSError:
    pass

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("VOICE SMOKE OK")
