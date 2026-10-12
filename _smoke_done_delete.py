# -*- coding: utf-8 -*-
"""Смоук-тест отображения выполненных задач и удаления."""
import os
import sys
import tempfile

tmp_db = os.path.join(tempfile.gettempdir(), "voiceplan_done_del.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(tmp_db + suffix)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = tmp_db
os.environ.setdefault("PORT", "18990")

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _sep, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

import bot  # noqa: E402

UID = 444555
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
            result = cur.fetchall() if fetch else None
            conn.commit()
            return result
        finally:
            conn.close()


db("INSERT OR IGNORE INTO users (user_id, balance_paid, balance_bonus) VALUES (?, 0, 0)", (UID,))
db("UPDATE users SET language='ru' WHERE user_id=?", (UID,))
check("user lang set to ru", bot.get_user_lang(UID) == "ru")

# --- 1. Паритет ключей RU=EN ---
ru_keys, en_keys = set(bot.LANGS["ru"]), set(bot.LANGS["en"])
check("RU=EN key parity", ru_keys == en_keys)
for k in ("task_delete_btn", "task_delete_confirm_title", "task_delete_confirm_body",
          "task_delete_yes", "task_delete_no"):
    check(f"key present: {k}", k in bot.LANGS["ru"] and k in bot.LANGS["en"])

# --- 2. Экранирование HTML ---
check("_esc escapes < > &", bot._esc("a<b>&c") == "a&lt;b&gt;&amp;c")

# --- 3. Создаём задачу и переключаем статус ---
res = bot.save_ai_result(UID, {"new_tasks": [{"text": "Купить молоко", "category": "📋 Повседневное"}]},
                         use_voice_limit=False)
tid = db("SELECT task_id FROM tasks WHERE user_id=? ORDER BY task_id DESC LIMIT 1", (UID,), fetch=True)[0]["task_id"]

row = bot._task_card(UID, tid)
card_active = bot.task_card_text(UID, row)
check("active card has no strikethrough", "<s>" not in card_active and "Купить молоко" in card_active)

# Отмечаем выполненной
db("UPDATE tasks SET is_completed=1 WHERE task_id=? AND user_id=?", (tid, UID))
row = bot._task_card(UID, tid)
card_done = bot.task_card_text(UID, row)
check("done card renders ✅ <s>текст</s> ✅",
      card_done.startswith("✅ <s>Купить молоко</s> ✅"))

# --- 4. Список папки показывает done-строку (функция сама читает БД) ---
ft_text, _ft_markup = bot.folder_tasks_text(UID, "📋 Повседневное")
check("folder list shows framed done task", "✅ <s>Купить молоко</s> ✅" in ft_text)

# --- 5. Карточка содержит кнопку удаления ---
markup = bot.task_card_markup(UID, row)
cb = [b.callback_data for r in markup.keyboard for b in r]
check("card has delete-ask button", any(c.startswith("taskdelask:") for c in cb))
check("card has done/undo button", any(c.startswith("tasktoggle:") for c in cb))

# --- 6. Удаление из БД ---
db("DELETE FROM tasks WHERE task_id=? AND user_id=?", (tid, UID))
left = db("SELECT COUNT(*) c FROM tasks WHERE user_id=?", (UID,), fetch=True)[0]["c"]
check("task deleted from DB", left == 0)

# --- 7. Заголовок без HTML не ломает экранирование ---
check("task_card_title still works", "📝" in bot.tr(UID, "task_card_title", title="X"))

print()
print("DONE/DELETE SMOKE OK" if not failures else "FAILED: " + ", ".join(failures))
sys.exit(1 if failures else 0)
