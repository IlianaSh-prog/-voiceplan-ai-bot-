# -*- coding: utf-8 -*-
"""Smoke антифлуда: скользящее окно, однократное предупреждение, восстановление,
админ-исключение, чистка памяти и порядок «флуд раньше оферты» в text_router.
Изолированная БД, отправка в Telegram перехвачена.
"""
import os
import sys
import tempfile
import time

tmp_db = os.path.join(tempfile.gettempdir(), "vp_flood.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(tmp_db + suffix)
    except FileNotFoundError:
        pass

os.environ["DB_PATH"] = tmp_db
os.environ.setdefault("PORT", "18995")
# Токен и админ обязательны для import bot (ADMIN_ID=0 → uid 0 будет админом).
os.environ["TELEGRAM_BOT_TOKEN"] = "0:test"
os.environ["ADMIN_ID"] = "0"
if os.path.exists(".env"):
    with open(".env", encoding="utf-8") as handle:
        for line in handle:
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.strip().split("=", 1)
                if key.strip() == "ADMIN_ID":
                    continue  # держим админа предсказуемым
                os.environ.setdefault(key.strip(), value.strip())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_msg(uid, text="привет"):
    return _Obj(chat=_Obj(id=uid), from_user=_Obj(id=uid), text=text,
                message_id=1, content_type="text")


sent = []


def fake_reply(message, text, reply_markup=None, **kw):
    sent.append(text)
    return _Obj(message_id=999)


bot.bot.reply_to = fake_reply
bot.bot.send_message = lambda *a, **k: sent.append(a[1] if len(a) > 1 else str(a)) or _Obj(message_id=100)

UID = 950101
ADMIN = bot.ADMIN_ID  # == 0 в этом смоуке

# 1) До лимита всё проходит.
results = [bot.flood_gate(make_msg(UID)) for _ in range(bot.FLOOD_MAX_EVENTS)]
check("first %d pass" % bot.FLOOD_MAX_EVENTS, results == [True] * bot.FLOOD_MAX_EVENTS, results)

# 2) Сверх лимита — блок, ровно одно предупреждение.
sent.clear()
over = [bot.flood_gate(make_msg(UID)) for _ in range(3)]
check("over-limit blocked", over == [False] * 3, over)
check("exactly one warning", len(sent) == 1, sent)
check("warning text is flood_warn", bool(sent) and "слишком часто" in sent[0].lower(), sent)

# 3) Второе предупреждение в том же окне не шлётся (ответы не флудим).
check("no repeated warning in window", len(sent) == 1, sent)

# 4) Окно истекло — доступ открыт, память почищена.
time.sleep(0.3)
old_window = bot.FLOOD_MAX_EVENTS  # noqa: F841 — фиксируем дефолт до подмены
bot.FLOOD_WINDOW_SEC = 0.2
time.sleep(0.25)
ok_again = bot.flood_gate(make_msg(UID))
check("recovered after window", ok_again is True, ok_again)
# Прямая проверка вытеснения: запись про простаившего юзера исчезает при чистке.
bot._flood_hits[950999] = [time.monotonic() - 999]
bot._flood_hits[950999] = [time.monotonic() - (bot.FLOOD_WINDOW_SEC + 5)]
bot.flood_gate(make_msg(950998))
check("stale user removed by cleanup", 950999 not in bot._flood_hits, list(bot._flood_hits))

# 5) Админ не ограничивается.
sent.clear()
admin_results = [bot.flood_gate(make_msg(ADMIN)) for _ in range(20)]
check("admin unlimited", admin_results == [True] * 20 and not sent,
      (sum(admin_results), len(sent)))

# 6) Интеграция: флуд важнее оферты — при флуде оферта даже не показывается.
bot.FLOOD_WINDOW_SEC = 10.0  # возвращаем нормальное окно
UID2 = 950102  # оферта НЕ принята
sent.clear()
for _ in range(bot.FLOOD_MAX_EVENTS):
    bot.text_router(make_msg(UID2))
oferta_before = len(sent)
check("normal flow offers oferta", oferta_before > 0, sent[:2])
for _ in range(3):
    bot.text_router(make_msg(UID2))
# Новых сообщений ровно одно — предупреждение о флуде (не оферта).
check("flood adds only one warning, no oferta",
      len(sent) == oferta_before + 1 and "слишком часто" in sent[-1].lower(),
      sent[oferta_before:])

print("\n%s" % ("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures)))
raise SystemExit(1 if failures else 0)