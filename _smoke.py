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

# 2б. Разовые подарки при регистрации (картинки/голосовые) НЕ сбрасываются
#     с новым днём — иначе бесплатка была бы накладной при наплыве юзеров.
with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute(
            "UPDATE users SET daily_image_left=0, daily_voice_left=0 "
            "WHERE user_id=?",
            (UID,),
        )
        conn.commit()
    finally:
        conn.close()
_paid, _bonus, _t, _c, images, _s = bot.get_balance(UID)
check("image gift not reset daily", images == 0)
check("voice gift not reset daily", bot.get_voice_quota(UID)[0] == 0)

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
# Кнопка Планировщика Pro присутствует на экране баланса.
check(
    "balance has Planner Pro button",
    any(
        (getattr(b, "callback_data", "") or "") == "bal:planner"
        for row in markup.keyboard for b in row
    ),
)
top_text, top_markup = bot.topup_screen(UID)
# 3 пакета + ЮKassa + «Назад».
check("topup_screen renders 5 buttons", len(top_markup.keyboard) == 5)
# Калькулятор «на что хватит» в пополнении.
check("topup has calculator", "хватит" in top_text and "запросов" in top_text)
# Экран Планировщика Pro.
pl_text, pl_markup = bot.planner_screen(UID)
check("planner_screen renders", "Планировщик" in pl_text)
check(
    "planner has buy button",
    any(
        (getattr(b, "callback_data", "") or "") == "bal:planner_buy"
        for row in pl_markup.keyboard for b in row
    ),
)
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
receipt = bot.tr(UID, "spend_receipt", amount="0.16", what="чат", total="2.00")
check("receipt renders amount only", "Списано" in receipt and "0.16" in receipt)
check("receipt has no tokens", "токен" not in receipt)
tries = bot.tr(UID, "tries_limit")
check("tries_limit mentions balance", "Проверьте баланс" in tries)

# 7а. Пофактовая тарификация чата (Вариант А, UnlimAI-стиль).
check("chat price floor (минималка)", bot.chat_price_rub(0, 0) == bot.CHAT_MIN_RUB)
check("chat price typical 900+300 tokens", bot.chat_price_rub(900, 300) == 0.16)
check("chat price cap (потолок)", bot.chat_price_rub(10_000_000, 10_000_000) == bot.CHAT_MAX_RUB)
check("AI_MARKUP default 5", bot.AI_MARKUP == 5)
check("CHAT_MIN 0.10 / CHAT_MAX 2", bot.CHAT_MIN_RUB == 0.10 and bot.CHAT_MAX_RUB == 2)
check("image price low default 3", bot.image_price_rub() == 3)
check("tts price per 1k stays 3", bot.PRICE_TTS_RUB_PER_1K == 3)
check("voice task price 3", bot.VOICE_TASK_PRICE_RUB == 3)

# 7в. Разовые подарки и лимиты бесплатного чата (новая схема бесплатки).
check(
    "registration gifts 1 image / 5 voice",
    (bot.FREE_IMAGES_ONCE, bot.FREE_VOICE_TASKS_ONCE) == (1, 5),
)
check(
    "free chat limits 2000 chars / 800 answer tokens",
    (bot.FREE_MSG_MAX_CHARS, bot.FREE_ANSWER_MAX_TOKENS) == (2000, 800),
)
check("paid answer ceiling 4000 tokens", bot.PAID_ANSWER_MAX_TOKENS == 4000)
check(
    "short mode 0.50 rub / 80 tokens",
    (bot.CHAT_SHORT_MODE_RUB, bot.SHORT_ANSWER_MAX_TOKENS) == (0.5, 80),
)
check(
    "msg_too_long RU/EN",
    "слишком длинный" in bot.LANGS["ru"]["msg_too_long"]
    and "too long" in bot.LANGS["en"]["msg_too_long"],
)
check(
    "short_mode_notice RU/EN",
    "лаконичные ответы" in bot.LANGS["ru"]["short_mode_notice"]
    and "short answers" in bot.LANGS["en"]["short_mode_notice"],
)
check(
    "free_gift_text mentions voice tasks",
    "голосовых задач" in bot.LANGS["ru"]["free_gift_text"]
    and "voice tasks" in bot.LANGS["en"]["free_gift_text"],
)

# 7б. Кэш озвучки: повторное прослушивание того же ответа бесплатно.
h1 = bot._tts_answer_hash("Ответ ИИ №1")
h2 = bot._tts_answer_hash("Ответ ИИ №2")
check("answer hash differs", h1 != h2)
bot.set_tts_cache(UID, h1, "fake-voice-file-id-1")
check("tts cache hit", bot.get_tts_cache(UID) == (h1, "fake-voice-file-id-1"))
bot.set_tts_cache(UID, h2, "fake-voice-file-id-2")
check("tts cache overwritten", bot.get_tts_cache(UID) == (h2, "fake-voice-file-id-2"))
with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute("DELETE FROM tts_cache WHERE user_id=?", (UID,))
        conn.commit()
    finally:
        conn.close()

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
