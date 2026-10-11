# -*- coding: utf-8 -*-
"""Смоук-тест устойчивости режима «Чат с ИИ» к перезапуску бота.

Раньше режим жил только в памяти процесса (user_states). После перезапуска
бота (Render засыпает/просыпается) режим терялся, и голос уходил в задачи.
Теперь режим дублируется в БД (users.ui_mode) — проверяем, что он выживает.
"""
import os
import sys
import tempfile

tmp_db = os.path.join(tempfile.gettempdir(), "voiceplan_smoke_chatmode.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(tmp_db + suffix)
    except FileNotFoundError:
        pass

os.environ["DB_PATH"] = tmp_db
try:
    os.environ.pop("ADMIN_ID", None)
except Exception:
    pass

# Читаем .env, чтобы подхватился реальный токен (TeleBot требует его на импорт),
# но не переопределяем уже заданные переменные (в т.ч. DB_PATH).
if os.path.exists(".env"):
    with open(".env", encoding="utf-8") as handle:
        for line in handle:
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.strip().split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

import bot  # noqa: E402

UID = 777002

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


bot.register_user(type("M", (), {"from_user": type(
    "U", (), {"id": UID, "username": "t", "first_name": "T", "language_code": "ru"})()})())
bot.init_db()

# Изначально режима нет.
check("ui_mode empty by default", bot.ui_mode(UID) == "")

# Входим в чат — режим пишется и в память, и в БД.
bot.enter_mode(UID, "chat")
check("enter_mode sets chat", bot.ui_mode(UID) == "chat")
check("ui_mode persisted to DB", bot.get_ui_mode(UID) == "chat")

# Эмуляция ПЕРЕЗАПУСКА бота: чистим только память процесса.
bot.user_states.clear()
check("memory cleared (restart)", bot.user_states.get(UID) is None)
check("chat survives restart via DB", bot.ui_mode(UID) == "chat")

# Голос в чате НЕ должен уходить в задачи: ветка handle_voice смотрит ui_mode.
check("voice would route to chat after restart", bot.ui_mode(UID) == "chat")

# Переключение на картинки — тоже устойчиво.
bot.enter_mode(UID, "image_prompt")
bot.user_states.clear()
check("image_prompt survives restart", bot.ui_mode(UID) == "image_prompt")

# Временный ввод (своё время/папка) выключает режим и в памяти, и в БД.
bot.enter_mode(UID, "chat")
bot.begin_input(UID, "category")
check("begin_input disables chat", bot.ui_mode(UID) == "")
check("begin_input clears DB mode", bot.get_ui_mode(UID) == "")

# Выход из чата чистит всё.
bot.enter_mode(UID, "chat")
bot.leave_mode(UID)
bot.user_states.clear()
check("leave_mode clears chat everywhere", bot.ui_mode(UID) == "")

if failures:
    print("\nCHATMODE SMOKE FAILED: " + ", ".join(failures))
    sys.exit(1)
print("\nCHATMODE SMOKE OK")
