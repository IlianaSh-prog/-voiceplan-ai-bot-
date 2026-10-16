# -*- coding: utf-8 -*-
"""Офлайн-смоук ПОЛНОГО пути «Чат с ИИ»: text_router → handle_plain_text →
process_chat_message. Отправки в Telegram перехватываются (подменяем send_message),
ИИ застаблен — проверяем чисто логику роутинга: при ui_mode='chat' должен уйти
статус «Обрабатываю» и ответ нейросети. Никакой реальной отправки.
"""
import os
import sqlite3
import tempfile

tmp_db = os.path.join(tempfile.gettempdir(), "vp_chatflow.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(tmp_db + suffix)
    except FileNotFoundError:
        pass

os.environ["DB_PATH"] = tmp_db
os.environ.setdefault("PORT", "18997")
if os.path.exists(".env"):
    with open(".env", encoding="utf-8") as handle:
        for line in handle:
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.strip().split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

import bot  # noqa: E402

UID = 999001
sent = []


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_msg(chat_id, uid, text, message_id=1):
    return _Obj(chat=_Obj(id=chat_id), from_user=_Obj(id=uid), text=text,
                message_id=message_id)


# Пользователь с балансом и дневной квотой, оферта принята.
conn = sqlite3.connect(tmp_db)
conn.execute(
    "INSERT INTO users (user_id, language, is_premium, oferta_accepted,"
    " balance_paid, daily_chat_left) VALUES (?, 'ru', 0, 1, 100.0, 5)",
    (UID,),
)
conn.commit()
conn.close()

# Перехватываем все исходящие вызовы — записываем вместо реальной отправки.
def fake_send(chat_id, text, reply_markup=None, reply_to_message_id=None, **kw):
    sent.append(("send", text))
    return make_msg(chat_id, UID, text, 100 + len(sent))


def fake_reply(message, text, reply_markup=None, **kw):
    sent.append(("reply", text))
    return make_msg(message.chat.id, UID, text, 100 + len(sent))


def fake_delete(chat_id, message_id):
    return True


def fake_edit(text, chat_id, message_id, **kw):
    sent.append(("edit", text))


bot.bot.send_message = fake_send
bot.bot.reply_to = fake_reply
bot.bot.delete_message = fake_delete
bot.bot.edit_message_text = fake_edit

# Детерминированный ИИ без сети (пофактовый чат возвращает (ответ, usage)).
bot.ai_chat_reply = lambda uid, text, max_tokens=None: (
    "ОТВЕТ_ИИ::" + text,
    {"prompt_tokens": 10, "completion_tokens": 5},
)

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# 1) Режим чата включён.
bot.enter_mode(UID, "chat")
check("ui_mode == chat", bot.ui_mode(UID) == "chat", bot.ui_mode(UID))

# 2) Вопрос текстом → должен уйти статус и ответ ИИ.
del sent[:]
bal_before = bot.get_balance(UID)[3]
question = make_msg(555, UID, "Как считается баланс?")
bot.text_router(question)

texts = [t for _k, t in sent]
has_status = any("Обрабатываю" in t or "Processing" in t or "обрабатываю" in t.lower()
                 for t in texts)
has_answer = any(t.startswith("ОТВЕТ_ИИ::") for t in texts)
check("chat sends a status ('Обрабатываю')", has_status, texts)
check("chat sends the AI answer", has_answer, texts)
check("no api_error emitted", not any("Ошибка" in t or "error" in t.lower() for t in texts), texts)

# 3) Квота списана после успешного ответа (ensure_daily мог сбросить её на
#    дневной дефолт — важен сам факт расхода, а не конкретное число).
_bal = bot.get_balance(UID)
check("daily chat quota consumed", _bal[3] < bal_before, (bal_before, "->", _bal[3]))

# 4) Выход из чата любым режим-независимым текстом НЕ ломает ничего.
bot.leave_mode(UID)
check("ui_mode cleared after leave", bot.ui_mode(UID) == "", bot.ui_mode(UID))

# 5) Бесплатный запрос длиннее FREE_MSG_MAX_CHARS — вежливый отказ без
#    расхода квоты (лимиты длины/токенов только у бесплатки).
bot.enter_mode(UID, "chat")
with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute("UPDATE users SET daily_chat_left=3 WHERE user_id=?", (UID,))
        conn.commit()
    finally:
        conn.close()
del sent[:]
bot.text_router(make_msg(555, UID, "а" * (bot.FREE_MSG_MAX_CHARS + 10)))
texts = [t for _k, t in sent]
check("too-long free message rejected", any("слишком длинный" in t for t in texts), texts)
check("long message burns no quota", bot.get_balance(UID)[3] == 3)

# 6) Лаконичный режим: квота 0, баланс ниже CHAT_SHORT_MODE_RUB → ответ
#    доставляется, но с подсказкой «пополните баланс», баланс не в минус.
with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute(
            "UPDATE users SET daily_chat_left=0, balance_paid=?, balance_bonus=0 "
            "WHERE user_id=?",
            (bot.CHAT_SHORT_MODE_RUB - 0.2, UID),
        )
        conn.commit()
    finally:
        conn.close()
del sent[:]
bot.text_router(make_msg(555, UID, "Коротко о балансе"))
texts = [t for _k, t in sent]
check("short mode: answer delivered", any(t.startswith("ОТВЕТ_ИИ::") for t in texts), texts)
check("short mode: notice shown", any("лаконичные ответы" in t for t in texts), texts)
_p, _b, *_ = bot.get_balance(UID)
check("short mode: balance not negative", _p + _b > 0, (_p, _b))
# 7) Режим защиты (/guard): с балансом — работает по предоплате, без баланса —
#    отказ без вызова ИИ и без расхода квоты.
bot.enter_mode(UID, "chat")
with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute(
            "UPDATE users SET balance_paid=100.0, balance_bonus=0, daily_chat_left=3 "
            "WHERE user_id=?", (UID,))
        conn.commit()
    finally:
        conn.close()
bot.set_guard(True)
del sent[:]
bot.text_router(make_msg(555, UID, "Guard: вопрос с балансом"))
texts = [t for _k, t in sent]
bal_after = sum(bot.get_balance(UID)[:2])
check("guard: paid user still gets AI answer",
      any(t.startswith("ОТВЕТ_ИИ::") for t in texts), texts)
check("guard: paid user spends balance", bal_after < 100.0, bal_after)

with bot.db_lock:
    conn = bot.get_db()
    try:
        conn.execute(
            "UPDATE users SET balance_paid=0, balance_bonus=0, daily_chat_left=3 "
            "WHERE user_id=?", (UID,))
        conn.commit()
    finally:
        conn.close()
del sent[:]
bot.text_router(make_msg(555, UID, "Guard: вопрос без баланса"))
texts = [t for _k, t in sent]
check("guard: no AI call without balance",
      not any(t.startswith("ОТВЕТ_ИИ::") for t in texts), texts)
check("guard: paused message shown",
      any("временно приостановлены" in t for t in texts), texts)
check("guard: free quota untouched", bot.get_balance(UID)[3] == 3,
      bot.get_balance(UID)[3])
bot.set_guard(False)
bot.leave_mode(UID)

print("\n%s" % ("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures)))
raise SystemExit(1 if failures else 0)
