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
# 1. ВЕБ-СЕРВЕР ДЛЯ RENDER (LIVE STATUS CHECK)
# =====================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"VoicePlan AI Enterprise Server is Live!")

    def log_message(self, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# =====================================================================
# 2. КОНФИГУРАЦИЯ И СЛОВАРИ ЯЗЫКОВ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.environ.get("AI_API_KEY") 
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.proxyapi.ru/openai/v1")

# ⚠️ УКАЖИТЕ ВАШ ЛИЧНЫЙ ТЕЛЕГРАМ ID (Можно узнать у бота @userinfobot)
ADMIN_ID = 8725167633 

# Ссылка на вашу оферту
OFFER_URL = "https://telegra.ph" 

if not TELEGRAM_TOKEN:
    print("[CRITICAL ERROR] Не задан TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
db_lock = threading.Lock()

SYSTEM_CATEGORIES_RU = ["🔴 Срочно", "📅 Календарь", "🛒 Покупки", "💡 Идеи", "📥 Входящие"]
SYSTEM_CATEGORIES_EN = ["🔴 Urgent", "📅 Calendar", "🛒 Groceries", "💡 Ideas", "📥 Inbox"]

user_states = {}

LANGS = {
    'ru': {
        'welcome': (
            "👋 **Здравствуйте, {name}!**\n\n"
            "Я — ваш персональный **ИИ-органайзер «VoicePlan AI»**.\n\n"
            "🎙 **Как со мной работать:**\n"
            "Просто наговорите голосовое сообщение с любым потоком мыслей — я сам вырежу слова-паразиты, "
            "разложу задачи по папкам и составлю наглядную таблицу!\n\n"
            "🎁 Вам начислено: **5 бесплатных голосовых разборов**."
        ),
        'btn_tasks': "📊 Моя Таблица Задач",
        'btn_folders': "📂 По папкам",
        'btn_new_cat': "➕ Новая категория",
        'btn_prem': "⭐ Премиум и Лимиты",
        'btn_support': "💬 Поддержка",
        'btn_lang': "🌐 Язык / Language",
        'empty_tasks': "📭 **Ваш список задач пуст!**\nНаговорите голосовое сообщение, чтобы добавить первые дела.",
        'dash_title': "📋 **ВАША ИНТЕРАКТИВНАЯ ТАБЛИЦА ДЕЛ:**\n──────────────────────────\n",
        'btn_done': "✅ Сделано",
        'btn_undone': "↩️ Вернуть",
        'folders_title': "📂 **ЗАДАЧИ ПО ПАПКАМ:**\n\n",
        'folders_empty': "Папки пока пусты. Наговорите или напишите задачи!",
        'new_cat_prompt': "✍️ **Введите название новой папки с эмодзи:**\n*(Например: 🚗 Автомобиль, 👶 Дети или 🏢 Проект)*",
        'new_cat_success': "✅ Категория **«{cat}»** успешно добавлена!",
        'support_text': "💬 **Служба поддержки VoicePlan AI**\n\nЕсли у вас возникли вопросы, напишите нашему администратору: @[Ваш_Логин]",
        'limits_text': (
            "⭐ **ВАШ ТАРИФ И ВОЗМОЖНОСТИ**\n\n"
            "Статус: **{status}**\n"
            "🎙 Оставшиеся бесплатные разборы: **{free} шт.**\n\n"
            "✨ **Преимущества Premium:**\n"
            "• Безлимитный ИИ-разбор голосовых заметок.\n"
            "• Голосовое закрытие задач прямо на лету.\n"
            "• Неограниченное число своих папок.\n\n"
            "📄 [Публичная оферта сервиса]({offer})"
        ),
        'lang_changed': "🌐 Язык успешно изменен на Русский 🇷🇺"
    },
    'en': {
        'welcome': (
            "👋 **Hello, {name}!**\n\n"
            "I am your personal **AI Organizer «VoicePlan AI»**.\n\n"
            "🎙 **How to use me:**\n"
            "Just send a voice note with your thoughts. I will remove filler words, "
            "categorize tasks, and build an interactive table!\n\n"
            "🎁 Free trials given: **5 pcs**."
        ),
        'btn_tasks': "📊 My Task Board",
        'btn_folders': "📂 Folders",
        'btn_new_cat': "➕ New Category",
        'btn_prem': "⭐ Premium & Limits",
        'btn_support': "💬 Support",
        'btn_lang': "🌐 Language / Язык",
        'empty_tasks': "📭 **Your task list is empty!**\nSend a voice note to add some tasks.",
        'dash_title': "📋 **YOUR INTERACTIVE TASK BOARD:**\n──────────────────────────\n",
        'btn_done': "✅ Done",
        'btn_undone': "↩️ Reopen",
        'folders_title': "📂 **TASKS BY FOLDERS:**\n\n",
        'folders_empty': "Your folders are empty. Send voice or text tasks!",
        'new_cat_prompt': "✍️ **Type a folder name with an emoji:**\n*(Example: 🚗 Car, 👶 Kids, 🏢 Project)*",
        'new_cat_success': "✅ Category **«{cat}»** created!",
        'support_text': "💬 **VoicePlan AI Support**\n\nIf you have any questions, please write to our support: @[Your_Username]",
        'limits_text': (
            "⭐ **YOUR PLAN & LIMITS**\n\n"
            "Status: **{status}**\n"
            "🎙 Free voice notes remaining: **{free} pcs**\n\n"
            "✨ **Premium Perks:**\n"
            "• Unlimited voice processing.\n"
            "• Voice-based task completion.\n"
            "• Unlimited custom categories.\n\n"
            "📄 [Public Offer Agreement]({offer})"
        ),
        'lang_changed': "🌐 Language successfully changed to English 🇬🇧"
    }
}

def get_text(uid, key, **kwargs):
    lang = get_user_lang(uid)
    text_template = LANGS.get(lang, LANGS['ru']).get(key, LANGS['ru'][key])
    return text_template.format(**kwargs)

# =====================================================================
# 3. БАЗА ДАННЫХ
# =====================================================================
def get_db():
    conn = sqlite3.connect("voiceplan_v4_3.db", timeout=60.0)
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
                amount_stars INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

init_db()

def register_user_if_not_exists(uid, username, full_name, lang_code):
    detected_lang = 'en' if lang_code and lang_code.startswith('en') else 'ru'
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (uid,))
        row = c.fetchone()
        is_new = False
        if not row:
            c.execute(
                "INSERT INTO users (user_id, username, full_name, language) VALUES (?, ?, ?, ?)",
                (uid, username or "NoUsername", full_name or "User", detected_lang)
            )
            conn.commit()
            is_new = True
        conn.close()
        
    if is_new:
        try:
            bot.send_message(
                ADMIN_ID,
                f"👤 **Новая регистрация в боте!**\n"
                f"Пользователь: {full_name} (@{username})\n"
                f"ID: `{uid}` | Язык: {detected_lang}",
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
# 4. КНОПКИ И ГЛАВНОЕ МЕНЮ
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
        types.KeyboardButton(get_text(uid, 'btn_lang'))
    )
    return markup

@bot.message_handler(commands=['start'])
def handle_start(message):
    uid = message.from_user.id
    register_user_if_not_exists(
        uid, 
        message.from_user.username, 
        message.from_user.first_name, 
        message.from_user.language_code
    )
    user_states.pop(uid, None)
    
    welcome_msg = get_text(uid, 'welcome', name=message.from_user.first_name)
    bot.send_message(uid, welcome_msg, reply_markup=get_main_keyboard(uid), parse_mode="Markdown")

@bot.message_handler(commands=['tasks'])
def cmd_tasks(message):
    show_dashboard(message)

@bot.message_handler(commands=['new_category'])
def cmd_new_cat(message):
    ask_new_category(message)

@bot.message_handler(commands=['premium'])
def cmd_prem(message):
    show_limits(message)

@bot.message_handler(commands=['support'])
def cmd_support(message):
    support_handler(message)

@bot.message_handler(commands=['lang'])
def cmd_lang(message):
    change_language_menu(message)

# =====================================================================
# 5. ТАБЛИЦА СВЕТОФОР (🔴 / 🟢) И ФУНКЦИЯ «ПО ПАПКАМ»
# =====================================================================
def build_task_dashboard(uid):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT task_id, category, task_text, is_completed FROM tasks WHERE user_id = ? ORDER BY is_completed ASC, task_id DESC", 
            (uid,)
        )
        rows = c.fetchall()
        conn.close()
        
    if not rows:
        return get_text(uid, 'empty_tasks'), None

    text = get_text(uid, 'dash_title') + "──────────────────────────\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    current_cat = None
    for tid, cat, ttext, is_done in rows:
        if cat != current_cat:
            current_cat = cat
            text += f"\n📂 **{current_cat}**\n"
            
        if is_done == 1:
            # 🟢 Сделано: зеленая кнопка + зачеркнутый текст
            text += f"🟢 ~{ttext}~\n"
            btn_label = f"{get_text(uid, 'btn_undone')}: {ttext[:22]}"
            markup.add(types.InlineKeyboardButton(btn_label, callback_data=f"toggle_{tid}"))
        else:
            # 🔴 Активно: ТОЛЬКО красный кружок, убрали текст "В работе"
            text += f"🔴 {ttext}\n"
            btn_label = f"{get_text(uid, 'btn_done')}: {ttext[:22]}"
            markup.add(types.InlineKeyboardButton(btn_label, callback_data=f"toggle_{tid}"))
            
    text += "\n──────────────────────────"
    return text, markup

@bot.message_handler(func=lambda msg: msg.text in [LANGS['ru']['btn_tasks'], LANGS['en']['btn_tasks']])
def show_dashboard(message):
    uid = message.from_user.id
    text, markup = build_task_dashboard(uid)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: msg.text in [LANGS['ru']['btn_folders'], LANGS['en']['btn_folders']])
def show_folders(message):
    uid = message.from_user.id
    categories = get_user_categories(uid)

    with db_lock:
        conn = get_db()
        c = conn.cursor()
        
        text = get_text(uid, 'folders_title')
        has_any_tasks = False
        
        for category in categories:
            c.execute(
                "SELECT task_text, is_completed FROM tasks WHERE user_id = ? AND category = ? ORDER BY is_completed ASC, task_id DESC", 
                (uid, category)
            )
            tasks = c.fetchall()
            
            if not tasks:
                continue
                
            has_any_tasks = True
            text += f"📁 **{category}**\n"
            for task, completed in tasks:
                icon = "🟢" if completed else "🔴"
                task_view = f"~{task}~" if completed else task
                text += f"{icon} {task_view}\n"
            text += "\n"
            
        conn.close()

    if not has_any_tasks:
        text += get_text(uid, 'folders_empty')

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

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
        
    bot.answer_callback_query(call.id, "✅")
    text, markup = build_task_dashboard(uid)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass

# =====================================================================
# 6. ИИ-ЯДРО (WHISPER + GPT-4O-MINI) С РАСПРЕДЕЛЕНИЕМ
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

def process_thoughts_with_gpt(raw_text, active_categories, user_open_tasks, uid):
    user_lang = get_user_lang(uid)
    url = f"{AI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-type": "application/json"
    }
    
    system_prompt = f"""
You are an intelligent personal assistant.
Analyze the user's speech/text and return strict JSON.
Target Language of response: {user_lang}.
The task text should be written in {user_lang}.

Available categories (DO NOT change their spelling, emojis, or language!): {json.dumps(active_categories, ensure_ascii=False)}
Open user tasks (to match if completed): {json.dumps(user_open_tasks, ensure_ascii=False)}

CRITICAL CLASSIFICATION RULES:
- "📅 Календарь" (or "📅 Calendar") is strictly for meetings, calls, events, and tasks with specific dates/times (e.g., "завтра в 10", "в четверг").
- "🛒 Покупки" (or "🛒 Groceries") is strictly for buying products, food, things, shopping lists.
- "🔴 Срочно" (or "🔴 Urgent") is strictly for high priority tasks that must be done immediately or today.
- "💡 Идеи" (or "💡 Ideas") is for thoughts, future plans, dreams, inspirations without specific action.
- "📥 Входящие" (or "📥 Inbox") is for generic tasks that do not fit into other categories (e.g. "убраться", "сделать педикюр").

Rules for completion:
1. If the user states they HAVE COMPLETED an open task from the list, return its ID in the "completed_task_ids" array.
2. Extract all NEW tasks/ideas, clean filler words, and place them into "new_tasks" array with "category" and "text".

Return ONLY JSON:
{{
  "completed_task_ids": [ids],
  "new_tasks": [
     {{"category": "EXACT_CATEGORY_NAME", "text": "cleaned_task_text"}}
  ]
}}
"""
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_text}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2
    }
    res = requests.post(url, headers=headers, json=body, timeout=30)
    if res.status_code == 200:
        try:
            return json.loads(res.json()['choices'][0]['message']['content'])
        except Exception:
            pass
    return {"completed_task_ids": [], "new_tasks": []}

@bot.message_handler(content_types=['voice'])
def handle_voice_note(message):
    uid = message.from_user.id
    free_left, is_prem = get_user_data(uid)
    
    if free_left <= 0 and is_prem == 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("⭐ Premium (199 Stars)", callback_data="buy_stars"),
            types.InlineKeyboardButton("💳 Картой РФ (ЮKassa)", callback_data="buy_yookassa")
        )
        bot.send_message(
            message.chat.id, 
            "🔒 **Бесплатный лимит разборов исчерпан.**\nПодключите Premium для безлимитной работы!", 
            reply_markup=markup, 
            parse_mode="Markdown"
        )
        return

    status_msg = bot.reply_to(message, "🎙 *Обрабатываю аудиопоток...*", parse_mode="Markdown")
    temp_file = f"temp_{uid}_{message.message_id}.ogg"
    
    try:
        file_info = bot.get_file(message.voice.file_id)
        file_data = bot.download_file(file_info.file_path)
        with open(temp_file, "wb") as f:
            f.write(file_data)
            
        raw_text = transcribe_voice_ogg(temp_file)
        
        if not raw_text.strip():
            bot.edit_message_text("❌ Не удалось распознать речь. Попробуйте записать еще раз.", message.chat.id, status_msg.message_id)
            return

        with db_lock:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT task_id, task_text FROM tasks WHERE user_id = ? AND is_completed = 0", (uid,))
            open_tasks = [{"id": r[0], "text": r[1]} for r in c.fetchall()]
            conn.close()

        cats = get_user_categories(uid)
        ai_res = process_thoughts_with_gpt(raw_text, cats, open_tasks, uid)
        
        completed_ids = ai_res.get("completed_task_ids", [])
        new_tasks = ai_res.get("new_tasks", [])
        
        closed_count = 0
        with db_lock:
            conn = get_db()
            c = conn.cursor()
            for cid in completed_ids:
                c.execute("UPDATE tasks SET is_completed = 1 WHERE task_id = ? AND user_id = ?", (cid, uid))
                closed_count += 1
            for nt in new_tasks:
                c.execute("INSERT INTO tasks (user_id, category, task_text) VALUES (?, ?, ?)", (uid, nt['category'], nt['text']))
            if is_prem == 0 and free_left > 0:
                c.execute("UPDATE users SET free_voice_left = free_voice_left - 1 WHERE user_id = ?", (uid,))
            conn.commit()
            conn.close()

        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, f"🗣 **Распознано:** _{raw_text}_", parse_mode="Markdown")
        
        t_text, t_markup = build_task_dashboard(uid)
        bot.send_message(message.chat.id, t_text, reply_markup=t_markup, parse_mode="Markdown")

    except Exception as e:
        bot.edit_message_text(f"⚠️ Ошибка обработки: {str(e)}", message.chat.id, status_msg.message_id)
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

# =====================================================================
# 8. ОБРАБОТКА ТЕКСТА И КАСТОМНЫЕ КАТЕГОРИИ
# =====================================================================
@bot.message_handler(func=lambda msg: not msg.text.startswith("/"))
def handle_text_messages(message):
    uid = message.from_user.id
    text_val = message.text.strip()
    
    # 1. Если пользователь вводит имя новой категории
    if user_states.get(uid) == "waiting_category_name":
        with db_lock:
            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT INTO custom_categories (user_id, category_name) VALUES (?, ?)", (uid, text_val))
            conn.commit()
            conn.close()
        user_states.pop(uid, None)
        bot.send_message(uid, get_text(uid, 'new_cat_success', cat=text_val), reply_markup=get_main_keyboard(uid), parse_mode="Markdown")
        return

    # 2. Переключение языков из меню
    if text_val in [LANGS['ru']['btn_lang'], LANGS['en']['btn_lang']]:
        change_language_menu(message)
        return

    # 3. Нажатие главных кнопок меню
    if text_val in [LANGS['ru']['btn_tasks'], LANGS['en']['btn_tasks']]:
        show_dashboard(message); return
    if text_val in [LANGS['ru']['btn_folders'], LANGS['en']['btn_folders']]:
        show_folders(message); return
    if text_val in [LANGS['ru']['btn_new_cat'], LANGS['en']['btn_new_cat']]:
        ask_new_category(message); return
    if text_val in [LANGS['ru']['btn_prem'], LANGS['en']['btn_prem']]:
        show_limits(message); return
    if text_val in [LANGS['ru']['btn_support'], LANGS['en']['btn_support']]:
        support_handler(message); return

    # 4. Ввод задачи текстом
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT task_id, task_text FROM tasks WHERE user_id = ? AND is_completed = 0", (uid,))
        open_tasks = [{"id": r[0], "text": r[1]} for r in c.fetchall()]
        conn.close()
        
    cats = get_user_categories(uid)
    ai_res = process_thoughts_with_gpt(text_val, cats, open_tasks, uid)
    
    completed_ids = ai_res.get("completed_task_ids", [])
    new_tasks = ai_res.get("new_tasks", [])
    
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        for cid in completed_ids:
            c.execute("UPDATE tasks SET is_completed = 1 WHERE task_id = ? AND user_id = ?", (cid, uid))
        for nt in new_tasks:
            c.execute("INSERT INTO tasks (user_id, category, task_text) VALUES (?, ?, ?)", (uid, nt['category'], nt['text']))
        conn.commit()
        conn.close()
        
    bot.send_message(uid, "✅ Задачи обновлены!", parse_mode="Markdown")
    t_text, t_markup = build_task_dashboard(uid)
    bot.send_message(uid, t_text, reply_markup=t_markup, parse_mode="Markdown")

def ask_new_category(message):
    uid = message.from_user.id
    user_states[uid] = "waiting_category_name"
    bot.send_message(message.chat.id, get_text(uid, 'new_cat_prompt'), parse_mode="Markdown")

def change_language_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en")
    )
    bot.send_message(message.chat.id, "🌐 **Выберите язык / Choose language:**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("setlang_"))
def callback_set_language(call):
    uid = call.from_user.id
    new_lang = call.data.replace("setlang_", "")
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET language = ? WHERE user_id = ?", (new_lang, uid))
        conn.commit()
        conn.close()
        
    bot.answer_callback_query(call.id, "✅")
    
    try:
        bot.edit_message_text(get_text(uid, 'lang_changed'), call.message.chat.id, call.message.message_id)
    except Exception:
        pass
        
    welcome_msg = get_text(uid, 'welcome', name=call.from_user.first_name)
    bot.send_message(uid, welcome_msg, reply_markup=get_main_keyboard(uid), parse_mode="Markdown")

def support_handler(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, get_text(uid, 'support_text'), parse_mode="Markdown")

# =====================================================================
# 9. ОПЛАТА (TELEGRAM STARS И ЮKASSA)
# =====================================================================
def show_limits(message):
    uid = message.from_user.id
    free_left, is_prem = get_user_data(uid)
    status_str = "🟢 Premium (Безлимит)" if is_prem == 1 else "⚪ Базовый"
    
    text = get_text(uid, 'limits_text', status=status_str, free=free_left, offer=OFFER_URL)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("⭐ Оплатить Telegram Stars (199 XTR)", callback_data="buy_stars"),
        types.InlineKeyboardButton("💳 Оплатить картой РФ — ЮKassa (299 ₽)", callback_data="buy_yookassa")
    )
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "buy_stars")
def callback_buy_stars(call):
    prices = [types.LabeledPrice(label="VoicePlan Premium (1 месяц)", amount=199)]
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="VoicePlan Premium",
        description="Безлимитный ИИ-планер и голосовое закрытие задач.",
        invoice_payload=f"stars_{call.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices
    )

@bot.pre_checkout_query_handler(func=lambda q: True)
def process_pre_checkout(q):
    bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    uid = message.from_user.id
    amount_stars = message.successful_payment.total_amount
    
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (uid,))
        c.execute("INSERT INTO payments (user_id, amount_stars, payment_system) VALUES (?, ?, ?)", (uid, amount_stars, 'Telegram Stars'))
        conn.commit()
        conn.close()
        
    bot.send_message(message.chat.id, "🎉 **Оплата прошла успешно!**\nВаш Premium-статус активирован!")
    
    try:
        user_info = bot.get_chat(uid)
        bot.send_message(
            ADMIN_ID,
            f"💰 **НОВАЯ ОПЛАТА В ЗВЕЗДАХ!**\n"
            f"Пользователь: {user_info.first_name} (@{user_info.username})\n"
            f"Сумма: **{amount_stars} XTR**",
            parse_mode="Markdown"
        )
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "buy_yookassa")
def callback_buy_yookassa(call):
    yookassa_pay_url = "https://yookassa.ru"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Оплатить 299 ₽ через ЮKassa", url=yookassa_pay_url))
    bot.send_message(call.message.chat.id, "Нажмите кнопку ниже для безопасной оплаты картой любого банка РФ:", reply_markup=markup)

# =====================================================================
# 10. АДМИН-ПАНЕЛЬ, АНАЛИТИКА И НАЧИСЛЕНИЕ ПОПЫТОК
# =====================================================================
@bot.message_handler(commands=['add_free'])
def add_free_attempts(message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.reply_to(
            message,
            "📝 **Формат команды:**\n`/add_free USER_ID КОЛИЧЕСТВО`\n\n"
            "*Пример:*\n`/add_free 123456789 10`",
            parse_mode="Markdown"
        )
        return

    user_id = int(parts[1])
    amount = int(parts[2])

    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET free_voice_left = free_voice_left + ? WHERE user_id = ?", (amount, user_id))
        changed = c.rowcount
        conn.commit()
        conn.close()

    if changed == 0:
        bot.reply_to(message, "❌ Пользователь с таким ID не найден в базе.")
    else:
        bot.reply_to(message, f"✅ Пользователю `{user_id}` успешно добавлено бесплатных разборов: **{amount}**.", parse_mode="Markdown")
        try:
            bot.send_message(user_id, f"🎁 **Вам начислено дополнительных голосовых разборов:** +{amount}!")
        except Exception:
            pass

@bot.message_handler(commands=['stats_all'])
def stats_all_command(message):
    if message.from_user.id != ADMIN_ID:
        return
        
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
        total_prem = c.fetchone()[0]
        
        c.execute("SELECT SUM(amount_stars) FROM payments")
        rev_stars = c.fetchone()[0]
        
        c.execute("SELECT language, COUNT(*) FROM users GROUP BY language")
        lang_stats = c.fetchall()
        
        conn.close()
        
    lang_text = "\n".join([f"• {row[0].upper()}: {row[1]} чел." for row in lang_stats]) or "Нет данных"
    
    text = (
        "📊 **АНАЛИТИКА ПРОЕКТА ЗА ВСЁ ВРЕМЯ (ALL-TIME):**\n\n"
        f"👥 Всего пользователей: **{total_users}**\n"
        f"⭐ Платящих (Premium): **{total_prem}**\n"
        f"⚪ Бесплатных: **{total_users - total_prem}**\n\n"
        f"🌍 **Языки аудитории:**\n{lang_text}\n\n"
        f"⭐ Выручка Telegram Stars: **{rev_stars or 0} XTR**"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['stats_period'])
def stats_period_command(message):
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
        
        c.execute("SELECT SUM(amount_stars) FROM payments WHERE created_at >= ?", (date_limit,))
        p_stars = c.fetchone()[0]
        
        conn.close()
        
    text = (
        f"📈 **АНАЛИТИКА ЗА ПОСЛЕДНИЕ {days} ДНЕЙ:**\n\n"
        f"👤 Новых пользователей: **{new_users}**\n"
        f"⭐ Доход Stars: **{p_stars or 0} XTR**"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

# =====================================================================
# 11. БЕЗОПАСНЫЙ ЗАПУСК И СБРОС СТАРЫХ ПОДКЛЮЧЕНИЙ
# =====================================================================
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        print("[INFO] Webhook успешно сброшен.")
    except Exception as e:
        print(f"[INFO] Предупреждение сброса webhook: {e}")
        
    print("🚀 Бот VoicePlan AI успешно запущен и готов к работе!")
    bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
