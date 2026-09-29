import os
import sys
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import telebot
from telebot import types

# =====================================================================
# 1. ВЕБ-СЕРВЕР ДЛЯ RENDER (LIVE STATUS)
# =====================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"VoicePlan AI Enterprise Server is Live!")
    def log_message(self, *args): return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# =====================================================================
# 2. КОНФИГУРАЦИЯ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.environ.get("AI_API_KEY") 
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.proxyapi.ru/openai/v1")

# ВАШ ID ИЗ ПРЕДЫДУЩЕГО СООБЩЕНИЯ
ADMIN_ID = 8725167633 
OFFER_URL = "https://graph.org/Oferta-VoicePlan-AI-09-29"

if not TELEGRAM_TOKEN:
    print("[ERROR] TELEGRAM_BOT_TOKEN не найден!")
    sys.exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
db_lock = threading.Lock()

SYSTEM_CATEGORIES_RU = ["🔴 Срочно", "📅 Календарь", "🛒 Покупки", "💡 Идеи", "📥 Входящие"]
SYSTEM_CATEGORIES_EN = ["🔴 Urgent", "📅 Calendar", "🛒 Groceries", "💡 Ideas", "📥 Inbox"]

user_states = {}

# =====================================================================
# 3. МУЛЬТИЯЗЫЧНЫЙ СЛОВАРЬ (i18n)
# =====================================================================
LANGS = {
    'ru': {
        'welcome': "👋 **Здравствуйте, {name}!**\n\nЯ — ваш персональный ИИ-органайзер «VoicePlan AI».\n🎙 Наговорите задачи голосом — я составлю таблицу!\n🎁 Бесплатных разборов: {free} шт.",
        'btn_tasks': "📊 Моя Таблица Задач",
        'btn_folders': "📂 По папкам",
        'btn_new_cat': "➕ Новая категория",
        'btn_prem': "⭐ Премиум и Лимиты",
        'btn_support': "💬 Поддержка",
        'btn_lang': "🌐 Язык / Language",
        'empty_tasks': "📭 **Ваш список задач пуст!**\nНаговорите голосовое сообщение, чтобы добавить первые дела.",
        'dash_title': "📋 **ВАША ТАБЛИЦА ДЕЛ:**\n",
        'btn_done': "✅ Сделано",
        'btn_undone': "↩️ Вернуть",
        'folders_title': "📂 **ЗАДАЧИ ПО ПАПКАМ:**\n\n",
        'folders_empty': "Папки пока пусты. Наговорите или напишите задачи!",
        'new_cat_prompt': "✍️ **Введите название новой папки с эмодзи:**",
        'new_cat_success': "✅ Категория **«{cat}»** добавлена!",
        'support_text': "💬 **Поддержка:** @[Ваш_Логин]\nПо всем вопросам пишите администратору.",
        'limits_text': "⭐ **ВАШ ТАРИФ**\nСтатус: {status}\n🎙 Попыток: {free} шт.\n\n📄 [Публичная оферта]({offer})",
        'lang_changed': "🌐 Язык успешно изменен на Русский 🇷🇺",
        'add_free_msg': "🎁 Вам начислено дополнительных голосовых разборов: +{amount}!"
    },
    'en': {
        'welcome': "👋 **Hello, {name}!**\n\nI am your **AI Organizer «VoicePlan AI»**.\n🎙 Send a voice note — I'll build your task board!\n🎁 Free trials: {free} pcs.",
        'btn_tasks': "📊 My Task Board",
        'btn_folders': "📂 Folders",
        'btn_new_cat': "➕ New Category",
        'btn_prem': "⭐ Premium & Limits",
        'btn_support': "💬 Support",
        'btn_lang': "🌐 Language / Язык",
        'empty_tasks': "📭 **Your task list is empty!**\nSend a voice note to capture your first tasks.",
        'dash_title': "📋 **YOUR TASK BOARD:**\n",
        'btn_done': "✅ Done",
        'btn_undone': "↩️ Reopen",
        'folders_title': "📂 **TASKS BY FOLDERS:**\n\n",
        'folders_empty': "Your folders are empty. Send voice or text tasks!",
        'new_cat_prompt': "✍️ **Type a folder name with an emoji:**",
        'new_cat_success': "✅ Category **«{cat}»** created!",
        'support_text': "💬 **Support:** @[Your_Username]\nContact us for any assistance.",
        'limits_text': "⭐ **YOUR PLAN**\nStatus: {status}\n🎙 Trials: {free} pcs\n\n📄 [Public Offer Agreement]({offer})",
        'lang_changed': "🌐 Language changed to English 🇬🇧",
        'add_free_msg': "🎁 Extra voice trials added: +{amount}!"
    }
}

def get_text(uid, key, **kwargs):
    lang = get_user_lang(uid)
    return LANGS.get(lang, LANGS['ru']).get(key, LANGS['ru'][key]).format(**kwargs)

# =====================================================================
# 4. БАЗА ДАННЫХ
# =====================================================================
def get_db():
    return sqlite3.connect("voiceplan_v5_5.db", timeout=60.0)

def init_db():
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, language TEXT DEFAULT "ru", free_voice_left INTEGER DEFAULT 5, is_premium INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        c.execute('CREATE TABLE IF NOT EXISTS custom_categories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category_name TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT, task_text TEXT, is_completed INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        # Добавлен столбец payment_system
        c.execute('CREATE TABLE IF NOT EXISTS payments (payment_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_stars INTEGER DEFAULT 0, amount_rub REAL DEFAULT 0, payment_system TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        conn.commit(); conn.close()

init_db()

def get_user_lang(uid):
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT language FROM users WHERE user_id = ?", (uid,))
        row = c.fetchone(); conn.close()
        return row[0] if row else 'ru'

def get_user_data(uid):
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT free_voice_left, is_premium FROM users WHERE user_id = ?", (uid,))
        row = c.fetchone(); conn.close()
        return row if row else (5, 0)

def get_user_categories(uid):
    lang = get_user_lang(uid)
    sys_cats = SYSTEM_CATEGORIES_EN if lang == 'en' else SYSTEM_CATEGORIES_RU
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT category_name FROM custom_categories WHERE user_id = ?", (uid,))
        custom = [r[0] for r in c.fetchall()]; conn.close()
    return sys_cats + custom

def register_user(uid, uname, fname, lang_code):
    det_lang = 'en' if lang_code and lang_code.startswith('en') else 'ru'
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (uid,))
        if not c.fetchone():
            c.execute("INSERT INTO users (user_id, username, full_name, language) VALUES (?, ?, ?, ?)", (uid, uname, fname, det_lang))
            conn.commit()
            try: bot.send_message(ADMIN_ID, f"👤 Новый юзер: {fname} (@{uname})")
            except: pass
        conn.close()

# =====================================================================
# 5. КЛАВИАТУРЫ И КОМАНДЫ
# =====================================================================
def get_main_keyboard(uid):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton(get_text(uid, 'btn_tasks')), types.KeyboardButton(get_text(uid, 'btn_folders')))
    markup.add(types.KeyboardButton(get_text(uid, 'btn_new_cat')), types.KeyboardButton(get_text(uid, 'btn_prem')))
    markup.add(types.KeyboardButton(get_text(uid, 'btn_support')), types.KeyboardButton(get_text(uid, 'btn_lang')))
    return markup

@bot.message_handler(commands=['start'])
def handle_start(message):
    uid = message.from_user.id
    register_user(uid, message.from_user.username, message.from_user.first_name, message.from_user.language_code)
    f, p = get_user_data(uid)
    bot.send_message(uid, get_text(uid, 'welcome', name=message.from_user.first_name, free=f), reply_markup=get_main_keyboard(uid), parse_mode="Markdown")

@bot.message_handler(commands=['tasks'])
def cmd_tasks(message): show_dashboard(message)

@bot.message_handler(commands=['add_free'])
def cmd_add_free(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        parts = message.text.split()
        t_id, amt = int(parts[1]), int(parts[2])
        with db_lock:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE users SET free_voice_left = free_voice_left + ? WHERE user_id = ?", (amt, t_id))
            conn.commit(); conn.close()
        bot.reply_to(message, f"✅ Начислено +{amt}")
        bot.send_message(t_id, get_text(t_id, 'add_free_msg', amount=amt))
    except: bot.reply_to(message, "Ошибка! /add_free ID AMT")

# =====================================================================
# 6. ТАБЛИЦА (БЕЗ СЛОВА "В РАБОТЕ") И ПАПКИ (С ЗАДАЧАМИ)
# =====================================================================
def build_task_dashboard(uid):
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT task_id, category, task_text, is_completed FROM tasks WHERE user_id = ? ORDER BY is_completed ASC, task_id DESC", (uid,))
        rows = c.fetchall(); conn.close()
    if not rows: return get_text(uid, 'empty_tasks'), None
    
    text = get_text(uid, 'dash_title') + "──────────────────\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    curr_cat = None
    for tid, cat, ttext, is_done in rows:
        if cat != curr_cat:
            curr_cat = cat
            text += f"\n📂 **{curr_cat}**\n"
        icon = "🟢" if is_done else "🔴"
        display = f"~{ttext}~" if is_done else ttext
        # ТУТ УДАЛЕНО СЛОВО "В РАБОТЕ"
        text += f"{icon} {display}\n"
        btn = f"{get_text(uid, 'btn_undone' if is_done else 'btn_done')}: {ttext[:20]}"
        markup.add(types.InlineKeyboardButton(btn, callback_data=f"tgl_{tid}"))
    return text, markup

@bot.message_handler(func=lambda msg: msg.text in [LANGS['ru']['btn_tasks'], LANGS['en']['btn_tasks']])
def show_dashboard(message):
    t, m = build_task_dashboard(message.from_user.id)
    bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text in [LANGS['ru']['btn_folders'], LANGS['en']['btn_folders']])
def show_folders(message):
    uid = message.from_user.id
    cats = get_user_categories(uid)
    with db_lock:
        conn = get_db(); c = conn.cursor()
        text = get_text(uid, 'folders_title')
        has = False
        for cat in cats:
            c.execute("SELECT task_text, is_completed FROM tasks WHERE user_id = ? AND category = ? ORDER BY is_completed", (uid, cat))
            tsks = c.fetchall()
            if tsks:
                has = True
                text += f"📁 **{cat}**\n"
                for t_txt, done in tsks:
                    text += f"{'🟢' if done else '🔴'} {f'~{t_txt}~' if done else t_txt}\n"
                text += "\n"
        conn.close()
    if not has: text = get_text(uid, 'folders_empty')
    bot.send_message(uid, text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("tgl_"))
def cb_toggle(call):
    tid = int(call.data.replace("tgl_", ""))
    uid = call.from_user.id
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT is_completed FROM tasks WHERE task_id = ? AND user_id = ?", (tid, uid))
        r = c.fetchone()
        if r:
            c.execute("UPDATE tasks SET is_completed = ? WHERE task_id = ?", (0 if r[0]==1 else 1, tid))
            conn.commit()
        conn.close()
    bot.answer_callback_query(call.id, "✅")
    t, m = build_task_dashboard(uid)
    try: bot.edit_message_text(t, call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode="Markdown")
    except: pass

# =====================================================================
# 7. ИИ (WHISPER + GPT-4O-MINI)
# =====================================================================
def ai_logic(uid, raw_text):
    lang = get_user_lang(uid)
    cats = get_user_categories(uid)
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT task_id, task_text FROM tasks WHERE user_id = ? AND is_completed = 0", (uid,))
        open_tsks = [{"id": r[0], "text": r[1]} for r in c.fetchall()]; conn.close()
    
    prompt = f"""Planner AI. Target Language: {lang}. 
    Categories: {cats}. Open tasks: {open_tsks}. 
    Rules:
    - If user says they finished something, put IDs in completed_task_ids.
    - New tasks to new_tasks. EXACT Category names!
    - 📅 Calendar: ONLY for dates/times.
    - 🛒 Groceries: ONLY for food/shopping.
    - 🔴 Urgent: strictly for TODAY/NOW.
    - 📥 Inbox: everything else (cleaning, pedicure, etc).
    Return JSON."""
    
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-type": "application/json"}
    body = {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": raw_text}], "response_format": {"type": "json_object"}}
    res = requests.post(AI_BASE_URL + "/chat/completions", headers=headers, json=body, timeout=35)
    return res.json()['choices'][0]['message']['content'] if res.status_code == 200 else '{"completed_task_ids":[], "new_tasks":[]}'

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    uid = message.from_user.id
    f, p = get_user_data(uid)
    if f <= 0 and not p:
        m = types.InlineKeyboardMarkup(); m.add(types.InlineKeyboardButton("⭐ Premium", callback_data="buy_p"))
        bot.reply_to(message, "🔒 Лимит исчерпан!", reply_markup=m); return
    
    m = bot.reply_to(message, "🎙 Обрабатываю...")
    fi = bot.get_file(message.voice.file_id)
    fd = bot.download_file(fi.file_path)
    with open("v.ogg", "wb") as f: f.write(fd)
    
    with open("v.ogg", "rb") as f:
        res = requests.post(AI_BASE_URL + "/audio/transcriptions", headers={"Authorization": f"Bearer {AI_API_KEY}"}, files={"file": ("v.ogg", f, "audio/ogg"), "model": (None, "whisper-1")})
    txt = res.json().get("text", "")
    os.remove("v.ogg")
    
    if not txt: bot.edit_message_text("❌ Не распознано.", uid, m.message_id); return
    
    data = json.loads(ai_logic(uid, txt))
    with db_lock:
        conn = get_db(); c = conn.cursor()
        for cid in data.get("completed_task_ids", []): c.execute("UPDATE tasks SET is_completed = 1 WHERE task_id = ?", (cid,))
        for nt in data.get("new_tasks", []): c.execute("INSERT INTO tasks (user_id, category, task_text) VALUES (?, ?, ?)", (uid, nt['category'], nt['text']))
        if not p: c.execute("UPDATE users SET free_voice_left = free_voice_left - 1 WHERE user_id = ?", (uid,))
        conn.commit(); conn.close()
    
    bot.delete_message(uid, m.message_id)
    bot.send_message(uid, f"🗣 **Распознано:** _{txt}_", parse_mode="Markdown")
    t, mkp = build_task_dashboard(uid)
    bot.send_message(uid, t, reply_markup=mkp, parse_mode="Markdown")

# =====================================================================
# 8. ЯЗЫК И ТЕКСТ (ИСПРАВЛЕНА СМЕНА ЯЗЫКА)
# =====================================================================
@bot.message_handler(func=lambda msg: not msg.text.startswith("/"))
def handle_text(message):
    uid = message.from_user.id
    val = message.text.strip()
    
    if user_states.get(uid) == "wait_c":
        with db_lock:
            conn = get_db(); c = conn.cursor()
            c.execute("INSERT INTO custom_categories (user_id, category_name) VALUES (?, ?)", (uid, val))
            conn.commit(); conn.close()
        user_states.pop(uid); bot.send_message(uid, get_text(uid, 'new_cat_success', cat=val), reply_markup=get_main_keyboard(uid)); return

    if val in [LANGS['ru']['btn_lang'], LANGS['en']['btn_lang']]:
        m = types.InlineKeyboardMarkup(); m.add(types.InlineKeyboardButton("🇷🇺 RU", callback_data="sl_ru"), types.InlineKeyboardButton("🇬🇧 EN", callback_data="sl_en"))
        bot.send_message(uid, "🌐 Выберите язык / Select Language:", reply_markup=m); return

    if val in [LANGS['ru']['btn_tasks'], LANGS['en']['btn_tasks']]: show_dashboard(message); return
    if val in [LANGS['ru']['btn_folders'], LANGS['en']['btn_folders']]: show_folders(message); return
    if val in [LANGS['ru']['btn_new_cat'], LANGS['en']['btn_new_cat']]: user_states[uid]="wait_c"; bot.send_message(uid, get_text(uid, 'new_cat_prompt')); return
    if val in [LANGS['ru']['btn_prem'], LANGS['en']['btn_prem']]:
        f, p = get_user_data(uid)
        m = types.InlineKeyboardMarkup(); m.add(types.InlineKeyboardButton("⭐ Stars", callback_data="buy_p"))
        bot.send_message(uid, get_text(uid, 'limits_text', status=("Premium" if p else "Base"), free=f, offer=OFFER_URL), reply_markup=m, parse_mode="Markdown"); return
    if val in [LANGS['ru']['btn_support'], LANGS['en']['btn_support']]: bot.send_message(uid, get_text(uid, 'support_text')); return

    # Текст как задача
    data = json.loads(ai_logic(uid, val))
    with db_lock:
        conn = get_db(); c = conn.cursor()
        for cid in data.get("completed_task_ids", []): c.execute("UPDATE tasks SET is_completed = 1 WHERE task_id = ?", (cid,))
        for nt in data.get("new_tasks", []): c.execute("INSERT INTO tasks (user_id, category, task_text) VALUES (?, ?, ?)", (uid, nt['category'], nt['text']))
        conn.commit(); conn.close()
    bot.send_message(uid, "✅"); t, m = build_task_dashboard(uid); bot.send_message(uid, t, reply_markup=m, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sl_"))
def cb_set_language(call):
    nl = call.data[3:]
    uid = call.from_user.id
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE users SET language = ? WHERE user_id = ?", (nl, uid))
        conn.commit(); conn.close()
    bot.answer_callback_query(call.id, "✅")
    # Мгновенно обновляем интерфейс
    bot.send_message(uid, LANGS[nl]['lang_changed'], reply_markup=get_main_keyboard(uid))
    f, p = get_user_data(uid)
    bot.send_message(uid, LANGS[nl]['welcome'].format(name=call.from_user.first_name, free=f), parse_mode="Markdown")

# =====================================================================
# 9. ОПЛАТА И СТАТИСТИКА
# =====================================================================
@bot.callback_query_handler(func=lambda call: call.data == "buy_p")
def cb_buy(call):
    bot.send_invoice(call.message.chat.id, "Premium", "Unlimited AI", "p_sub", "", "XTR", [types.LabeledPrice("Premium", 199)])

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_check(q): bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def success_pay(message):
    uid = message.from_user.id
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (uid,))
        c.execute("INSERT INTO payments (user_id, amount_stars, payment_system) VALUES (?, ?, ?)", (uid, 199, "Stars"))
        conn.commit(); conn.close()
    bot.send_message(uid, "🎉 Premium Activated!")
    try: bot.send_message(ADMIN_ID, f"💰 Оплата от {message.from_user.first_name}")
    except: pass

@bot.message_handler(commands=['stats_all'])
def admin_stats(message):
    if message.from_user.id != ADMIN_ID: return
    with db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(is_premium) FROM users")
        u, p = c.fetchone()
        c.execute("SELECT SUM(amount_stars) FROM payments")
        rev = c.fetchone()[0] or 0
        conn.close()
    bot.reply_to(message, f"📊 Всего: {u}\n⭐ Прем: {p}\n💰 Доход: {rev} XTR")

@bot.message_handler(commands=['stats_period'])
def admin_period(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        days = int(message.text.split()[1])
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (since,))
            n = c.fetchone()[0]
            conn.close()
        bot.reply_to(message, f"📈 За {days} дн.: {n} новых юзеров")
    except: pass

# =====================================================================
# 10. ЗАПУСК
# =====================================================================
if __name__ == "__main__":
    try: bot.remove_webhook()
    except: pass
    print("🚀 VoicePlan AI Enterprise Live!")
    bot.infinity_polling(skip_pending=True)
