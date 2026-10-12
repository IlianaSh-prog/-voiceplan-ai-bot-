# -*- coding: utf-8 -*-
"""Smoke для админ-команды /setbalance (изолированная БД)."""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "777001"
_tmpdb = os.path.join(tempfile.gettempdir(), f"vp_bal_{os.getpid()}.db")
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


ADMIN = bot.ADMIN_ID

# Не дёргаем реальный Telegram в тесте.
bot.bot.reply_to = lambda *a, **k: None
bot.bot.send_message = lambda *a, **k: None


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMsg:
    def __init__(self, text, uid):
        self.text = text
        self.message_id = 1
        self.from_user = type("U", (), {"id": uid})()
        self.chat = FakeChat(uid)

    def reply_to(self, *a, **k):
        return None


def set_bal(text):
    """Вызывает admin_set_balance и возвращает новое состояние баланса."""
    bot.admin_set_balance(FakeMsg(text, ADMIN))
    paid, bonus, *_ = bot.get_balance(ADMIN)
    return round(paid, 2), round(bonus, 2)


db("INSERT INTO users (user_id, language) VALUES (?, 'ru')", (ADMIN,))

# 1. Установить платный баланс 500, бонус 0.
check("set 500/0", set_bal("/setbalance 500") == (500.0, 0.0))
# 2. Обнулить.
check("set 0/0", set_bal("/setbalance 0") == (0.0, 0.0))
# 3. Платный + бонус.
check("set 100/50", set_bal("/setbalance 100 50") == (100.0, 50.0))
# 4. Дробное через запятую.
check("set 250,75/0", set_bal("/setbalance 250,75") == (250.75, 0.0))
# 5. Не число -> баланс не меняется.
before = db("SELECT balance_paid, balance_bonus FROM users WHERE user_id=?", (ADMIN,), fetch=True)[0]
bot.admin_set_balance(FakeMsg("/setbalance abc", ADMIN))
after = db("SELECT balance_paid, balance_bonus FROM users WHERE user_id=?", (ADMIN,), fetch=True)[0]
check("bad input ignored", (before["balance_paid"], before["balance_bonus"]) ==
      (after["balance_paid"], after["balance_bonus"]))
# 6. Отрицательное/слишком большое -> не меняется.
bot.admin_set_balance(FakeMsg("/setbalance -5", ADMIN))
neg = db("SELECT balance_paid FROM users WHERE user_id=?", (ADMIN,), fetch=True)[0]["balance_paid"]
check("negative rejected", neg == before["balance_paid"])
bot.admin_set_balance(FakeMsg("/setbalance 9999999", ADMIN))
big = db("SELECT balance_paid FROM users WHERE user_id=?", (ADMIN,), fetch=True)[0]["balance_paid"]
check("too big rejected", big == before["balance_paid"])
# 7. Не-админ не меняет (вызов не должен уронить процесс).
bot.admin_set_balance(FakeMsg("/setbalance 999", 999999))
u = db("SELECT balance_paid FROM users WHERE user_id=999", fetch=True)
check("non-admin no row created", not u)
# 8. Текст баланса больше не содержит «Цены:».
bal_text, _ = bot.balance_screen(ADMIN)
check("balance text has no price line", "Цены:" not in bal_text and "цена" not in bal_text.lower())
# 9. Озвучки явно помечены как разовые (не ежедневные).
check("voice line says one-time", "не обновляются" in bal_text and "один раз" in bal_text)
check("daily label separate from voice", "Бесплатно каждый день" in bal_text)

db("DELETE FROM users WHERE user_id=?", (ADMIN,))
try:
    os.remove(_tmpdb)
except OSError:
    pass

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("SETBALANCE SMOKE OK")
