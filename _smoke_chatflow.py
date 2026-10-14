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

# Детерминированный ИИ без сети.
bot.ai_chat_reply = lambda uid, text: "ОТВЕТ_ИИ::" + text

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

print("\n%s" % ("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures)))
raise SystemExit(1 if failures else 0)
