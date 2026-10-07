# -*- coding: utf-8 -*-
"""Smoke-тест балансной логики: дневные нормы, списание, пробные озвучки."""
import os
import sys
import tempfile

# Изолированная БД и свой порт, чтобы не трогать работающего бота.
tmp_db = os.path.join(tempfile.gettempdir(), "voiceplan_smoke.db")
if os.path.exists(tmp_db):
    os.remove(tmp_db)
os.environ["DB_PATH"] = tmp_db
os.environ.setdefault("PORT", "18999")

# Загрузка токена из .env (bot.py требует TELEGRAM_BOT_TOKEN при импорте).
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _sep, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

import bot  # noqa: E402

UID = 999999
failures = []


def check(name, condition):
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failures.append(name)


with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (UID,))
        conn.commit()
    finally:
        conn.close()

# 1. Дневные нормы по умолчанию.
_paid, _bonus, tts, chats, images, show = bot.get_balance(UID)
check("daily defaults 3/1/3", (chats, images, tts) == (3, 1, 3))
check("show_spends default on", show == 1)

# 2. Списание дневной нормы и сброс при смене дня.
bot.consume_daily(UID, "chat")
_paid, _bonus, _t, chats, _i, _s = bot.get_balance(UID)
check("chat quota 3 -> 2", chats == 2)

with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute("UPDATE users SET free_day='2000-01-01' WHERE user_id=?", (UID,))
        conn.commit()
    finally:
        conn.close()
_paid, _bonus, _t, chats, _i, _s = bot.get_balance(UID)
check("quota reset on new day", chats == 3)

# 3. Списание: сначала бонусный, затем платный.
with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute(
            "UPDATE users SET balance_paid=10, balance_bonus=2 WHERE user_id=?",
            (UID,),
        )
        conn.commit()
    finally:
        conn.close()
check("spend 5 within balance", bot.spend_from_balance(UID, 5) is True)
paid, bonus, *_rest = bot.get_balance(UID)
check("bonus spent first (0 left)", abs(bonus - 0.0) < 1e-6)
check("paid after bonus spend = 7", abs(paid - 7.0) < 1e-6)
check("overdraft rejected", bot.spend_from_balance(UID, 100) is False)

# 4. Пробные озвучки.
bot.consume_tts_trial(UID)
_paid, _bonus, tts, *_rest = bot.get_balance(UID)
check("tts trial 3 -> 2", tts == 2)

# 5. Экраны рендерятся без KeyError.
text, markup = bot.balance_screen(UID)
check("balance_screen renders", "Баланс" in text and len(markup.keyboard) == 6)
top_text, top_markup = bot.topup_screen(UID)
# 3 пакета + ЮKassa + «Назад».
check("topup_screen renders 5 buttons", len(top_markup.keyboard) == 5)
check(
    "packages 199/490/990",
    [p[0] for p in bot.TOPUP_PACKAGES] == [199, 490, 990],
)
check("no day-streak package", all(p[0] != 7 for p in bot.TOPUP_PACKAGES))
check("490 is popular", bot.TOPUP_PACKAGES[1] == (490, 5))
check("990 bonus 10%", bot.TOPUP_PACKAGES[2] == (990, 10))

# 6. tries_limit ведёт на кнопку баланса.
markup_lim = bot.balance_open_markup(UID)
check("tries_limit -> balance button", markup_lim.keyboard[0][0].callback_data == "bal:show")

# 7. Квитанция и тексты.
receipt = bot.tr(UID, "spend_receipt", amount="5.00", what="чат", total="2.00")
check("receipt renders", "Списано" in receipt)
tries = bot.tr(UID, "tries_limit")
check("tries_limit mentions balance", "Проверьте баланс" in tries)

with bot.db_lock:
    conn = bot.get_db()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    finally:
        conn.close()
expected = {
    "balance_paid", "balance_bonus", "tts_free_left",
    "free_day", "daily_chat_left", "daily_image_left", "show_spends",
}
check("all balance columns exist", expected <= cols)

os.remove(tmp_db)
print()
if failures:
    print(f"SMOKE FAILED: {len(failures)} checks")
    sys.exit(1)
print("SMOKE OK")
