import os
import sys
import json
import sqlite3
import threading
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
        self.wfile.write(b"VoicePlan AI Enterprise is running!")

    def log_message(self, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# =====================================================================
# 2. КОНФИГУРАЦИЯ И СЛОВАРИ ЯЗЫКОВ (i18n)
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.environ.get("AI_API_KEY") 
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.proxyapi.ru/openai/v1")

# Настройки ЮKassa (Получить в личном кабинете ЮKassa)
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "123456")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "test_secret_key")

ADMIN_ID = 8725167633  # ⚠️ Вставьте ваш Telegram ID
OFFER_URL = "https://telegra.ph/Oferta-VoicePlan"

if not TELEGRAM_TOKEN:
    print("[ERROR] Не задан TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
db_lock = threading.Lock()

SYSTEM_CATEGORIES_RU = ["🔴 Срочно", "📅 Календарь", "🛒 Покупки", "💡 Идеи", "📥 Входящие"]
SYSTEM_CATEGORIES_EN = ["🔴 Urgent", "📅 Calendar", "🛒 Groceries", "💡 Ideas", "📥 Inbox"]
user_states = {}

# Мультиязычный словарь интерфейса
LANGS = {
    'ru': {
        'welcome': "👋 Здравствуйте, {name}!\n\nЯ — ваш персональный **ИИ-органайзер «VoicePlan AI»**.\n🎙 Наговорите сумбурный поток мыслей — я составлю таблицу!\n🎁 Начислено бесплатных разборов: 5 шт.",
        'btn_tasks': "📊 Моя Таблица Задач",
        'btn_folders': "📂 По папкам",
        'btn_new_cat': "➕ Новая категория",
        'btn_prem': "⭐ Премиум и Лимиты",
        'btn_support': "💬 Поддержка",
        'empty_tasks': "📭 Ваш список задач пуст!\nНаговорите голосовое сообщение, чтобы добавить первые дела.",
        'dash_title': "📋 **ВАША ИНТЕРАКТИВНАЯ ТАБЛИЦА ДЕЛ:**\n──────────────────────────\n",
        'status_done': "🟢 [Готово]",
        'status_todo': "🔴 [В работе]",
        'btn_done': "✅ Сделано",
        'btn_undone': "↩️ Вернуть",
        'lang_changed': "🌐 Язык успешно изменен на Русский 🇷🇺"
    },
    'en': {
        'welcome': "👋 Hello, {name}!\n\nI am your personal **AI Organizer «VoicePlan AI»**.\n🎙 Send a voice note with your chaotic thoughts — I'll build a board!\n🎁 Free voice notes given: 5 pcs.",
        'btn_tasks': "📊 My Task Board",
        'btn_folders': "📂 Folders",
        'btn_new_cat': "➕ New Category",
        'btn_prem': "⭐ Premium & Limits",
        'btn_support': "💬 Support",
        'empty_tasks': "📭 Your task list is empty!\nSend a voice note to add your first tasks.",
        'dash_title': "📋 **YOUR INTERACTIVE TASK BOARD:**\n──────────────────────────\n",
        'status_done': "🟢 [Done]",
        'status_todo': "🔴 [In Progress]",
        'btn_done': "✅ Done",
        'btn_undone': "↩️ Reopen",
        'lang_changed': "🌐 Language successfully changed to English 🇬🇧"
    }
}

def get_text(uid, key, **kwargs):
    lang = get_user_lang(uid)
    text_template = LANGS.get(lang, LANGS['ru']).get(key, LANGS['ru'][key])
    return text_template.format(**kwargs)

# =====================================================================
# 3. БАЗА ДАННЫХ SQLITE
# =====================================================================
def get_db():
    conn = sqlite3.connect("voiceplan_enterprise.db", timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                language TEXT DEFAULT 'ru',
                country TEXT DEFAULT 'Россия',
                free_voice_left INTEGER DEFAULT 5,
                is_premium INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS custom_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category_name TEXT
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category TEXT,
                task_text TEXT,
                is_completed INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount_rub REAL DEFAULT 0,
                amount_stars INTEGER DEFAULT 0,
                payment_system TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

init_db()

def register_user(uid, uname, fname, lang_code):
    detected_lang = 'en' if lang_code and lang_code.startswith('en') else 'ru'
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id, language FROM users WHERE user_id = ?", (uid,))
        existing = c.fetchone()
        if not existing:
            c.execute("INSERT INTO users (user_id, username, full_name, language) VALUES (?, ?, ?, ?)",
                      (uid, uname or "NoUsername", fname or "User", detected_lang))
            conn.commit()
            is_new = True
        else:
            is_new = False
        conn.close()
    
    if is_new:
        try:
            bot.send_message(
                ADMIN_ID,
                f"👤 **Новый пользователь зарегистрирован!**\n"
                f"Имя: {fname} (@{uname})\nID: `{uid}`\nЯзык: {detected_lang}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

def get_user_lang(uid):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT language FROM users WHERE user_id = ?", (uid,))
        row = c.fetchone()
        conn.close()
        return row[0] if row and row[0] in ['ru', 'en'] else 'ru'

def get_user_data(uid):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT free_voice_left, is_premium FROM users WHERE user_id = ?", (uid,))
        row = c.fetchone()
        conn.close()
        return row if row else (5, 0)

def get_user_categories(uid):
    lang = get_user_lang(uid)
    sys_cats = SYSTEM_CATEGORIES_EN if lang == 'en' else SYSTEM_CATEGORIES_RU
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT category_name FROM custom_categories WHERE user_id = ?", (uid,))
        custom = [r[0] for r in c.fetchall()]
        conn.close()
    return sys_cats + custom

# =====================================================================
# 4. МЕНЮ И СМЕНА ЯЗЫКА
# =====================================================================
def get_main_keyboard(uid):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton(get_text(uid, 'btn_tasks')),
        types.KeyboardButton(get_text(uid, 'btn_folders'))
    )
    markup.add(
        types.KeyboardButton(get_text(uid, 'btn_new_cat')),
        types.KeyboardButton(get_text(uid, 'btn_prem'))
    )
    markup.add(
        types.KeyboardButton(get_text(uid, 'btn_support')),
        types.KeyboardButton("🌐 Язык / Language")
    )
    return markup

@bot.message_handler(commands=['start'])
def handle_start(message):
    uid = message.from_user.id
    register_user(uid, message.from_user.username, message.from_user.first_name, message.from_user.language_code)
    user_states.pop(uid, None)
    
    text = get_text(uid, 'welcome', name=message.from_user.first_name)
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard(uid), parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text == "🌐 Язык / Language")
def change_language_menu(message):
    uid = message.from_user.id
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
    )
    bot.send_message(message.chat.id, "🌐 **Выберите язык интерфейса / Select your language:**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def callback_set_lang(call):
    uid = call.from_user.id
    new_lang = call.data.replace("lang_", "")
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET language = ? WHERE user_id = ?", (new_lang, uid))
        conn.commit()
        conn.close()
        
    bot.answer_callback_query(call.id, "OK")
    bot.edit_message_text(get_text(uid, 'lang_changed'), call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=get_main_keyboard(uid))

# =====================================================================
# 5. ИНТЕРАКТИВНАЯ ТАБЛИЦА (СВЕТОФОР 🟢/🔴)
# =====================================================================
def build_task_dashboard(uid):
    lang = get_user_lang(uid)
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT task_id, category, task_text, is_completed FROM tasks WHERE user_id = ? ORDER BY is_completed ASC, task_id DESC", (uid,))
        rows = c.fetchall()
        conn.close()
        
    if not rows:
        return get_text(uid, 'empty_tasks'), None

    text = get_text(uid, 'dash_title')
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    current_cat = None
    for tid, cat, ttext, is_done in rows:
        if cat != current_cat:
            current_cat = cat
            text += f"\n📂 **{current_cat}**\n"
            
        if is_done == 1:
            text += f"🟢 {get_text(uid, 'status_done')} ~{ttext}~\n"
            markup.add(types.InlineKeyboardButton(f"{get_text(uid, 'btn_undone')}: {ttext[:20]}", callback_data=f"toggle_{tid}"))
        else:
            text += f"🔴 {get_text(uid, 'status_todo')} {ttext}\n"
            markup.add(types.InlineKeyboardButton(f"{get_text(uid, 'btn_done')}: {ttext[:20]}", callback_data=f"toggle_{tid}"))
            
    text += "\n──────────────────────────"
    return text, markup

@bot.message_handler(func=lambda msg: msg.text in ["📊 Моя Таблица Задач", "📊 My Task Board"])
def show_dashboard(message):
    uid = message.from_user.id
    text, markup = build_task_dashboard(uid)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_"))
def callback_toggle_task(call):
    tid = int(call.data.replace("toggle_", ""))
    uid = call.from_user.id
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT is_completed FROM tasks WHERE task_id = ? AND user_id = ?", (tid, uid))
        row = c.fetchone()
        if row:
            new_status = 0 if row[0] == 1 else 1
            c.execute("UPDATE tasks SET is_completed = ? WHERE task_id = ?", (new_status, tid))
            conn.commit()
        conn.close()
        
    bot.answer_callback_query(call.id, "OK")
    text, markup = build_task_dashboard(uid)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass

# =====================================================================
# 6. ИИ-АНАЛИЗАТОР (WHISPER + GPT-4O-MINI)
# =====================================================================
def transcribe_voice_ogg(file_path):
    if not AI_API_KEY:
        return ""
    url = f"{AI_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    with open(file_path, "rb") as f:
        files = {"file": ("voice.ogg", f, "audio/ogg"), "model": (None, "whisper-1")}
        res = requests.post(url, headers=headers, files=files, timeout=60)
    if res.status_code == 200:
        return res.json().get("text", "")
    return ""

def process_thoughts_with_gpt(raw_text, active_categories, user_open_tasks):
    url = f"{AI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-type": "application/json"}
    
    system_prompt = f"""
You are a smart personal assistant. Analyze the user's stream of thoughts and return strict JSON.
Available categories: {json.dumps(active_categories, ensure_ascii=False)}
Open tasks: {json.dumps(user_open_tasks, ensure_ascii=False)}

Rules:
1. If user says they completed a task, put its ID in "completed_task_ids".
2. Extract new tasks and put them into "new_tasks" with category and text.
3. Clean speech parasites.

Return ONLY JSON:
{{
  "completed_task_ids": [ids],
  "new_tasks": [{{"category": "name", "text": "task"}}]
}}
"""
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": raw_text}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2
    }
    res = requests.post(url, headers=headers, json=body, timeout=30)
    if res.status_code == 200:
        return json.loads(res.json()['choices'][0]['message']['content'])
    return {"completed_task_ids": [], "new_tasks": []}

@bot.message_handler(content_types=['voice'])
def handle_voice_note(message):
    uid = message.from_user.id
    free_left, is_prem = get_user_data(uid)
    
    if free_left <= 0 and is_prem == 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⭐ Premium (199 Stars)", callback_data="buy_stars"),
                   types.InlineKeyboardButton("💳 Картой РФ (ЮKassa)", callback_data="buy_yookassa"))
        bot.send_message(message.chat.id, "🔒 **Лимит бесплатных разборов исчерпан.**\nПодключите Premium!", reply_markup=markup, parse_mode="Markdown")
        return

    status_msg = bot.reply_to(message, "🎙 *Обрабатываю аудиопоток...*", parse_mode="Markdown")
    try:
        file_info = bot.get_file(message.voice.file_id)
        file_data = bot.download_file(file_info.file_path)
        temp_file = f"v_{uid}_{message.message_id}.ogg"
        with open(temp_file, "wb") as f:
            f.write(file_data)
            
        raw_text = transcribe_voice_ogg(temp_file)
        if os.path.exists(temp_file):
            os.remove(temp_file)
            
        if not raw_text.strip():
            bot.edit_message_text("❌ Не удалось распознать голос.", message.chat.id, status_msg.message_id)
            return

        with db_lock:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT task_id, task_text FROM tasks WHERE user_id = ? AND is_completed = 0", (uid,))
            open_tasks = [{"id": r[0], "text": r[1]} for r in c.fetchall()]
            conn.close()

        cats = get_user_categories(uid)
        ai_res = process_thoughts_with_gpt(raw_text, cats, open_tasks)
        
        completed_ids = ai_res.get("completed_task_ids", [])
        new_tasks = ai_res.get("new_tasks", [])
        
        closed_count = 0
        with db_lock:
            conn = get_db()
            c = conn.cursor()
            for cid in completed_ids:
                c.execute("UPDATE tasks SET is_completed = 1 WHERE task_id = ? AND user_id = ?", (cid, uid))
                closed_count += 1
            for t in new_tasks:
                c.execute("INSERT INTO tasks (user_id, category, task_text) VALUES (?, ?, ?)", (uid, t['category'], t['text']))
            if is_prem == 0 and free_left > 0:
                c.execute("UPDATE users SET free_voice_left = free_voice_left - 1 WHERE user_id = ?", (uid,))
            conn.commit()
            conn.close()

        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, f"🗣 Распознано: _{raw_text}_", parse_mode="Markdown")
        
        t_text, t_markup = build_task_dashboard(uid)
        bot.send_message(message.chat.id, t_text, reply_markup=t_markup, parse_mode="Markdown")
    except Exception as e:
        bot.edit_message_text(f"⚠️ Ошибка: {str(e)}", message.chat.id, status_msg.message_id)

# =====================================================================
# 7. ДОБАВЛЕНИЕ КАТЕГОРИЙ И ПОДДЕРЖКА
# =====================================================================
@bot.message_handler(func=lambda msg: msg.text in ["➕ Новая категория", "➕ New Category"])
def ask_new_category(message):
    user_states[message.from_user.id] = "waiting_category_name"
    bot.send_message(message.chat.id, "✍️ Введите название новой папки/категории с эмодзи:")

@bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id) == "waiting_category_name")
def save_new_category(message):
    cat_name = message.text.strip()
    uid = message.from_user.id
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO custom_categories (user_id, category_name) VALUES (?, ?)", (uid, cat_name))
        conn.commit()
        conn.close()
    user_states.pop(uid, None)
    bot.send_message(message.chat.id, f"✅ Категория «{cat_name}» добавлена!", reply_markup=get_main_keyboard(uid))

@bot.message_handler(func=lambda msg: msg.text in ["💬 Поддержка", "💬 Support"])
def support_handler(message):
    bot.send_message(
        message.chat.id,
        "💬 **Служба поддержки VoicePlan AI**\n\nЕсли у вас возникли вопросы или проблемы с оплатой, напишите нашему администратору: @[Ваш_логин]",
        parse_mode="Markdown"
    )

# =====================================================================
# 8. ОПЛАТА (TELEGRAM STARS + ЮKASSA) И ОТЧЕТЫ АДМИНУ
# =====================================================================
@bot.message_handler(func=lambda msg: msg.text in ["⭐ Премиум и Лимиты", "⭐ Premium & Limits"])
def show_limits(message):
    uid = message.from_user.id
    free_left, is_prem = get_user_data(uid)
    status_str = "🟢 Premium (Безлимит)" if is_prem == 1 else "⚪ Базовый"
    
    text = (
        f"⭐ **СТАТУС ПОДПИСКИ**\nСтатус: **{status_str}**\n"
        f"🎙 Осталось бесплатных разборов: {free_left} шт.\n\n"
        f"📄 [Публичная оферта]({OFFER_URL})"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("⭐ Оплатить через Telegram Stars (199 XTR)", callback_data="buy_stars"),
        types.InlineKeyboardButton("💳 Оплатить картой РФ — ЮKassa (299 ₽)", callback_data="buy_yookassa")
    )
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "buy_stars")
def callback_stars(call):
    prices = [types.LabeledPrice(label="VoicePlan Premium", amount=199)]
    bot.send_invoice(call.message.chat.id, "VoicePlan Premium", "Безлимитный ИИ-планер", "sub_stars", "", "XTR", prices=prices)

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_check(q):
    bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def success_pay(message):
    uid = message.from_user.id
    amount = message.successful_payment.total_amount
    payload = message.successful_payment.invoice_payload
    
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (uid,))
        c.execute("INSERT INTO payments (user_id, amount_stars, payment_system) VALUES (?, ?, ?)", (uid, amount, 'Telegram Stars'))
        conn.commit()
        conn.close()
        
    bot.send_message(message.chat.id, "🎉 Оплата прошла успешно! Премиум активирован.")
    try:
        user_info = bot.get_chat(uid)
        bot.send_message(ADMIN_ID, f"💰 **ОПЛАТА ЧЕРЕЗ STARS!**\nПользователь: {user_info.first_name} (@{user_info.username})\nСумма: {amount} XTR", parse_mode="Markdown")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "buy_yookassa")
def callback_yookassa(call):
    # Генерация ссылки ЮKassa (заглушка под боевой API)
    pay_url = "https://yookassa.ru/my/i/demo_link" 
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Перейти к оплате 299 ₽", url=pay_url))
    bot.send_message(call.message.chat.id, "Нажмите кнопку ниже для безопасной оплаты картой РФ через ЮKassa:", reply_markup=markup)

# =====================================================================
# 9. СУПЕР-АНАЛИТИКА ДЛЯ ВЛАДЕЛЬЦА (/stats_all и /stats_period)
# =====================================================================
@bot.message_handler(commands=['stats_all'])
def stats_all(message):
    if message.from_user.id != ADMIN_ID:
        return
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
        total_prem = c.fetchone()[0]
        c.execute("SELECT SUM(amount_rub), SUM(amount_stars) FROM payments")
        rev_rub, rev_stars = c.fetchone()
        conn.close()
        
    text = (
        "📊 **АНАЛИТИКА ЗА ВСЁ ВРЕМЯ (ALL-TIME):**\n\n"
        f"👥 Всего пользователей: **{total_users}**\n"
        f"⭐ Платящих (Премиум): **{total_prem}**\n"
        f"⚪ Бесплатных: **{total_users - total_prem}**\n\n"
        f"💵 Выручка в рублях (ЮKassa): **{rev_rub or 0} ₽**\n"
        f"⭐ Выручка в звездах (Stars): **{rev_stars or 0} XTR**"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['stats_period'])
def stats_period(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
    
    date_limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (date_limit,))
        new_users = c.fetchone()[0]
        c.execute("SELECT SUM(amount_rub), SUM(amount_stars) FROM payments WHERE created_at >= ?", (date_limit,))
        p_rub, p_stars = c.fetchone()
        conn.close()
        
    text = (
        f"📈 **АНАЛИТИКА ЗА ПОСЛЕДНИЕ {days} ДНЕЙ:**\n\n"
        f"👤 Новых регистраций: **{new_users}**\n"
        f"💵 Доход в рублях: **{p_rub or 0} ₽**\n"
        f"⭐ Доход в звездах: **{p_stars or 0} XTR**"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

# =====================================================================
# 10. ЗАПУСК
# =====================================================================
if __name__ == "__main__":
    bot.infinity_polling()
