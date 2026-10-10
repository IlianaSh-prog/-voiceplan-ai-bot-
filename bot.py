import os
import sys
import json
import time
import base64
import tempfile
import uuid
import calendar as calmod
import sqlite3
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException


# ============================================================
# 1. ЛОГИРОВАНИЕ И HEALTH CHECK ДЛЯ RENDER
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("voiceplan")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"VoicePlan is running")

    def log_message(self, *_args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server listening on port %s", port)
    server.serve_forever()


threading.Thread(target=run_health_server, daemon=True).start()


# ============================================================
# 2. НАСТРОЙКИ
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.environ.get("AI_API_KEY")
AI_BASE_URL = os.environ.get(
    "AI_BASE_URL",
    "https://api.proxyapi.ru/openai/v1"
).rstrip("/")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
OFFER_URL = os.environ.get("OFFER_URL", "")

# Сохраните старое имя базы, чтобы не потерять существующие данные.
DB_PATH = os.environ.get("DB_PATH", "voiceplan_v4_3.db")

# Необязательный TTS (озвучка ответов). По умолчанию — тот же шлюз proxyapi,
# что и чат с картинками: /audio/speech, OpenAI-compatible.
# Стоимость ≈ $15/1M знаков ≈ 1.5 ₽ за 1000 знаков (типичный ответ 0.5–1 ₽).
TTS_API_URL = os.environ.get("TTS_API_URL", "").strip() or f"{AI_BASE_URL}/audio/speech"
TTS_API_KEY = os.environ.get("TTS_API_KEY", "").strip() or (AI_API_KEY or "")
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")
TTS_PRICE_RUB_PER_1K = float(os.environ.get("TTS_PRICE_RUB_PER_1K", "1.5"))

# Стоимость запросов для админ-отчёта (₽ за 1 млн токенов / за минуту Whisper).
# Значения по умолчанию соответствуют gpt-4o-mini; можно переопределить в env.
LLM_PRICE_IN_RUB = float(os.environ.get("LLM_PRICE_IN_RUB_PER_1M", "15"))
LLM_PRICE_OUT_RUB = float(os.environ.get("LLM_PRICE_OUT_RUB_PER_1M", "60"))
WHISPER_PRICE_RUB_PER_MIN = float(os.environ.get("WHISPER_PRICE_RUB_PER_MIN", "1.5"))

# Нейросети нового интерфейса: чат и картинки (модели и цены настраиваются в env).
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
IMAGE_PRICE_RUB = float(os.environ.get("IMAGE_PRICE_RUB", "3"))

# Балансная модель: цены для пользователя и дневные бесплатные нормы.
PRICE_CHAT_RUB = float(os.environ.get("PRICE_CHAT_RUB", "1"))
PRICE_IMAGE_RUB = float(os.environ.get("PRICE_IMAGE_RUB", "5"))
PRICE_TTS_RUB_PER_1K = float(os.environ.get("PRICE_TTS_RUB_PER_1K", "1"))
FREE_CHATS_PER_DAY = int(os.environ.get("FREE_CHATS_PER_DAY", "3"))
FREE_IMAGES_PER_DAY = int(os.environ.get("FREE_IMAGES_PER_DAY", "1"))
# Пробные озвучки выдаются один раз при подключении (не ежедневно).
FREE_TTS_TRIES = int(os.environ.get("FREE_TTS_TRIES", "3"))
# Пакеты пополнения через Telegram Stars: (сумма ₽, бонус %).
# «Серию дней» и другие ежедневные подарки сознательно не делаем —
# постоянные списания «в подарок» невыгодны при бесплатных пользователях.
TOPUP_PACKAGES = ((199, 0), (490, 5), (990, 10))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная TELEGRAM_BOT_TOKEN")

if not AI_API_KEY:
    logger.warning("AI_API_KEY не задан. Голосовая расшифровка и ИИ-разбор не будут работать.")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
db_lock = threading.RLock()
user_states = {}

SYSTEM_CATEGORIES = {
    "ru": ["🔴 Срочно", "📅 Календарь", "🛒 Покупки", "💡 Идеи", "📥 Входящие"],
    "en": ["🔴 Urgent", "📅 Calendar", "🛒 Groceries", "💡 Ideas", "📥 Inbox"],
}

LANGS = {
    "ru": {
        "welcome": (
            "👋 Здравствуйте, {name}!\n\n"
            "Я — VoicePlan: чат с ИИ, генерация картинок и голосовой планировщик задач — "
            "в одном боте. Выберите действие в меню внизу.\n\n"
            "🆓 Бесплатно сегодня: чат {chats}, картинки {images}.\n"
            "💰 Баланс: {total} ₽."
        ),
        "btn_tasks": "📋 Мои задачи",
        "btn_events": "📅 События",
        "btn_folders": "📂 По папкам",
        "btn_new_cat": "➕ Новая папка",
        "btn_settings": "⚙️ Настройки",
        "btn_language": "🌐 Язык / Language",
        "btn_premium": "💎 Баланс и оплата",
        "btn_support": "💬 Поддержка",
        "btn_done": "✅ Сделано",
        "btn_undone": "↩️ Вернуть",
        "btn_calendar": "🗓 Календарь",
        "dash_title": "📋 Ваши задачи:",
        "empty_tasks": "📭 Задач пока нет. Напишите или наговорите, что нужно сделать.",
        "empty_events": "📭 Событий пока нет.",
        "folders_empty": "В папках пока нет задач.",
        "language_prompt": "Выберите язык интерфейса:",
        "language_saved": "✅ Язык интерфейса изменён.",
        "settings": "⚙️ Настройки уведомлений и озвучки:",
        "voice_on": "🔊 Озвучка ответов: включена",
        "voice_off": "🔇 Озвучка ответов: выключена",
        "reminders": "⏰ Напоминания: {value}",
        "reminders_none": "не выбраны",
        "cal_day_reminders": "   ⏰ напомню: {value}",
        "timezone": "🌍 Часовой пояс: {value}",
        "choose_timezone": (
            "Введите часовой пояс из базы IANA, например:\n"
            "`Europe/Moscow` или `Asia/Novosibirsk`"
        ),
        "choose_reminders": "Выберите интервалы напоминаний. Можно отметить несколько:",
        "category_prompt": "Введите название новой папки:",
        "category_saved": "✅ Папка «{name}» создана.",
        "processing": "🎙 Обрабатываю сообщение…",
        "api_error": "⚠️ Не удалось обработать запрос. Попробуйте ещё раз чуть позже.",
        "no_speech": "Не удалось распознать речь. Попробуйте записать сообщение ещё раз.",
        "task_added": "✅ Задачи и события обновлены.",
        "remind_pick": "📌 «{title}» сохранено. Выберите время напоминания:",
        "remind_custom_btn": "✍️ Своё время",
        "remind_skip": "🚫 Без напоминания",
        "remind_custom_prompt": "✍️ Введите время напоминания, например: «за 30 минут», «за 2 часа» или «за 1 день».",
        "remind_custom_bad": "Не удалось распознать время. Примеры: «за 30 минут», «за 2 часа», «за 1 день».",
        "remind_done": "✅ Напоминание установлено: {value}.",
        "remind_skipped": "🚫 Напоминание не установлено.",
        "reminder": "⏰ Скоро событие: {title}\n🕒 {date}",
        "reminder_options": "Напоминания: {value}",
        "event_card": (
            "📅 Событие: {title}\n"
            "🕒 {date}\n"
            "⏰ Напоминания: {reminders}"
        ),
        "premium": "🎙 Бесплатных голосовых разборов осталось: {free}.",
        "not_admin": "Команда доступна только администратору.",
        "grant_usage": "Формат: `/add_free USER_ID КОЛИЧЕСТВО`",
        "grant_ok": "✅ Добавлено попыток: {count}.",
        "grant_user": "🎁 Вам начислено голосовых разборов: +{count}.",
        "support": "💬 По вопросам поддержки напишите: {contact}",
        "unknown_timezone": "Не удалось распознать часовой пояс. Проверьте написание, например Europe/Moscow.",
        "confirm_event": "Событие сохранено.",
        "cal_title": "🗓 Календарь — {month}",
        "cal_legend": "✳ — день, в котором есть события. Нажмите на дату внизу, чтобы посмотреть.",
        "cal_no_dates": "В этом месяце событий нет.",
        "cal_day_title": "📅 {date}:",
        "cal_day_empty": "📭 В этот день событий нет.",
        "cal_today": "📍 Сегодня",
        "free_usage": "Формат: /free 10 — начислить попытки себе.\nБез аргументов — показать остаток.",
        "free_added": "🎁 Начислено попыток: +{count}. Всего теперь: {free}.",
        "btn_chat": "💬 Чат с ИИ",
        "btn_image": "🎨 Картинки",
        "btn_video": "🎬 Видео",
        "btn_oferta": "📖 Оферта",
        "btn_exit_chat": "↩️ Выйти из чата",
        "oferta_accept_btn": "✅ Принимаю условия",
        "oferta_required": (
            "⚠️ Чтобы пользоваться ботом, сначала примите условия оферты — "
            "нажмите кнопку ниже."
        ),
        "oferta_accepted_msg": "✅ Условия приняты! Добро пожаловать 🎉",
        "oferta_text": (
            "📜 УСЛОВИЯ ПУБЛИЧНОЙ ОФЕРТЫ\n\n"
            "1. Сервис — ИИ-ассистент VoicePlan: чат с нейросетью, генерация картинок, "
            "планировщик задач и календарь с напоминаниями.\n"
            "2. Бесплатно: новые пользователи получают дневные лимиты чата и картинок "
            "и пробные озвучки; лимиты обновляются 1 раз в день. Планировщик, задачи "
            "и календарь — без лимитов.\n"
            "3. Платные функции (чат, картинки, озвучка) оплачиваются балансом через "
            "Telegram Stars в разделе «💎 Баланс и оплата». Автопродления нет — "
            "баланс пополняется по желанию.\n"
            "4. Возврат — по обращению в поддержку: неиспользованный баланс "
            "возвращаем полностью; при сбоях сервиса обращайтесь в поддержку.\n"
            "5. Ответы ИИ носят информационный характер и не заменяют профессиональную "
            "консультацию.\n"
            "6. Данные (имя, ID, тексты запросов) используются только для работы сервиса "
            "и передаются его провайдерам (Telegram, ИИ-API). Рекламе третьих лиц "
            "не продаются.\n"
            "7. Запрещено: противоправные цели, спам, обход лимитов. За нарушение "
            "доступ может быть ограничен.\n"
            "8. Сервис предоставляется «как есть», возможны перерывы в работе.\n"
            "9. Поддержка: {contact}.\n\n"
            "Нажимая «Принимаю условия», вы подтверждаете согласие (акцепт публичной "
            "оферты по ст. 437 ГК РФ) и начинаете пользоваться сервисом."
        ),
        "chat_started": (
            "💬 Чат с ИИ включён — задавайте вопрос.\n"
            "Выйти: кнопка «↩️ Выйти из чата» или любая кнопка меню."
        ),
        "chat_exited": "✅ Чат закрыт. Отправляйте планы — разберу на задачи и события.",
        "image_started": "🎨 Пришлите описание картинки одним сообщением — нарисую.",
        "image_generating": "🎨 Генерирую картинку…",
        "image_done": "🎨 Готово! Бесплатных картинок сегодня осталось: {left}.",
        "image_done_paid": "🎨 Готово!",
        "tries_limit": (
            "⛔ Бесплатные попытки на сегодня закончились.\n"
            "Проверьте баланс — кнопка ниже. "
            "Бесплатные возможности обновляются 1 раз в день."
        ),
        "bal_open": "💰 Посмотреть баланс",
        "balance_text": (
            "💰 Баланс\n"
            "Всего: {total} ₽\n"
            "Платный: {paid} ₽\n"
            "Бонусный: {bonus} ₽\n\n"
            "🆓 Бесплатно сегодня:\n"
            "• Чат: {chats} из {chats_max}\n"
            "• Картинки: {images} из {images_max}\n"
            "• Пробные озвучки: {tts_free}\n\n"
            "Бесплатные возможности обновляются 1 раз в день.\n"
            "Цены: чат {price_chat} ₽, картинка {price_image} ₽, "
            "озвучка {price_tts} ₽/1000 знаков.\n"
            "Планировщик, задачи и календарь — бесплатно навсегда."
        ),
        "bal_topup": "💰 Пополнить баланс",
        "bal_free_gift": "🎁 Получить баланс Бесплатно",
        "bal_how": "ℹ️ Как работает баланс",
        "bal_save": "💡 Как экономить баланс",
        "bal_spends_on": "✅ Показывать списания — выключить",
        "bal_spends_off": "🔕 Показывать списания — включить",
        "bal_close": "❌ Закрыть",
        "bal_back": "⬅️ Назад",
        "popular": "🔥 ПОПУЛЯРНОЕ",
        "topup_title": (
            "💰 Пополнение баланса\n"
            "Выберите пакет (оплата через Telegram Stars):"
        ),
        "topup_done": (
            "✅ На баланс зачислено {amount} ₽ (+{bonus} ₽ бонуса).\n"
            "Всего: {total} ₽"
        ),
        "free_gift_text": (
            "🎁 Бесплатно:\n"
            "• Каждый день: {chats} чата и {images} картинка — "
            "обновляются 1 раз в день.\n"
            "• Новым пользователям: {tts} пробные озвучки ответов.\n"
            "• Планировщик, задачи и календарь — всегда бесплатно."
        ),
        "how_balance_text": (
            "ℹ️ Как работает баланс:\n"
            "• Бесплатные возможности обновляются 1 раз в день.\n"
            "• Планировщик — бесплатно навсегда."
        ),
        "save_balance_text": (
            "💡 Как экономить баланс:\n"
            "• Ловите дневную бесплатную норму: {chats} чата и {images} "
            "картинка каждый день.\n"
            "• Пишите вопросы коротко и по делу.\n"
            "• Озвучку включайте только когда нужно слушать.\n"
            "• Задачи и планы ведите в планировщике — он бесплатен."
        ),
        "spends_on": "✅ Списания теперь показываются",
        "spends_off": "🔕 Списания больше не показываются",
        "spend_receipt": "💳 Списано {amount} ₽ ({what}). Остаток: {total} ₽.",
        "spend_chat": "чат",
        "spend_image": "картинка",
        "spend_tts": "озвучка",
        "tts_locked": (
            "🔇 Пробные озвучки закончились. Озвучка включится при "
            "положительном балансе ({price} ₽/1000 знаков)."
        ),
        "video_stub": "🎬 Генерация видео уже в разработке — появится здесь совсем скоро!",
        "yookassa_stub": "💳 Оплата через ЮKassa пока не подключена. Совсем скоро!",
        "btn_planner": "📋 Задачи и календарь",
        "nav_folders": "📂 Папки",
        "nav_calendar": "🗓 Календарь",
        "nav_reminders": "⏰ Напоминания",
        "nav_events": "📅 События",
        "nav_newcat": "➕ Новая папка",
        "nav_tasks": "⬅️ К задачам",
        "report_usage": "Формат отчёта:\n/report 01.10.2026 10:00 05.10.2026 23:59\nили без времени: /report 01.10 05.10",
    },
    "en": {
        "welcome": (
            "👋 Hello, {name}!\n\n"
            "I’m VoicePlan: AI chat, image generation and a voice task planner — "
            "all in one bot. Pick an action from the menu below.\n\n"
            "🆓 Free today: chat {chats}, images {images}.\n"
            "💰 Balance: {total} RUB."
        ),
        "btn_tasks": "📋 My tasks",
        "btn_events": "📅 Events",
        "btn_folders": "📂 Folders",
        "btn_new_cat": "➕ New folder",
        "btn_settings": "⚙️ Settings",
        "btn_language": "🌐 Language / Язык",
        "btn_premium": "💎 Balance & pay",
        "btn_support": "💬 Support",
        "btn_done": "✅ Done",
        "btn_undone": "↩️ Undo",
        "btn_calendar": "🗓 Calendar",
        "dash_title": "📋 Your tasks:",
        "empty_tasks": "📭 No tasks yet. Send or type something you need to do.",
        "empty_events": "📭 No events yet.",
        "folders_empty": "There are no tasks in your folders yet.",
        "language_prompt": "Choose your interface language:",
        "language_saved": "✅ Interface language updated.",
        "settings": "⚙️ Reminder and voice settings:",
        "voice_on": "🔊 Spoken replies: on",
        "voice_off": "🔇 Spoken replies: off",
        "reminders": "⏰ Reminders: {value}",
        "reminders_none": "none selected",
        "cal_day_reminders": "   ⏰ reminders: {value}",
        "timezone": "🌍 Time zone: {value}",
        "choose_timezone": (
            "Enter an IANA time zone, for example:\n"
            "`Europe/London` or `America/New_York`"
        ),
        "choose_reminders": "Choose reminder intervals. You may select several:",
        "category_prompt": "Enter a name for the new folder:",
        "category_saved": "✅ Folder “{name}” created.",
        "processing": "🎙 Processing your message…",
        "api_error": "⚠️ I couldn’t process that request. Please try again shortly.",
        "no_speech": "I couldn’t recognize the speech. Please record another message.",
        "task_added": "✅ Tasks and events updated.",
        "remind_pick": "📌 “{title}” saved. Choose a reminder time:",
        "remind_custom_btn": "✍️ Custom time",
        "remind_skip": "🚫 No reminder",
        "remind_custom_prompt": "✍️ Enter a reminder time, e.g. “30 minutes before”, “2 hours before” or “1 day before”.",
        "remind_custom_bad": "Couldn’t recognize the time. Examples: “30 minutes before”, “2 hours before”, “1 day before”.",
        "remind_done": "✅ Reminder set: {value}.",
        "remind_skipped": "🚫 No reminder set.",
        "reminder": "⏰ Upcoming event: {title}\n🕒 {date}",
        "reminder_options": "Reminders: {value}",
        "event_card": (
            "📅 Event: {title}\n"
            "🕒 {date}\n"
            "⏰ Reminders: {reminders}"
        ),
        "premium": "🎙 Free voice analyses left: {free}.",
        "not_admin": "This command is available to the administrator only.",
        "grant_usage": "Format: `/add_free USER_ID COUNT`",
        "grant_ok": "✅ Added attempts: {count}.",
        "grant_user": "🎁 You received extra voice analyses: +{count}.",
        "support": "💬 For support, contact: {contact}",
        "unknown_timezone": "Unknown time zone. Use a valid IANA name, for example Europe/London.",
        "confirm_event": "Event saved.",
        "cal_title": "🗓 Calendar — {month}",
        "cal_legend": "✳ — a day with events. Tap a date below to view them.",
        "cal_no_dates": "No events this month.",
        "cal_day_title": "📅 {date}:",
        "cal_day_empty": "📭 No events on this day.",
        "cal_today": "📍 Today",
        "free_usage": "Format: /free 10 — add attempts to yourself.\nWithout arguments — show balance.",
        "free_added": "🎁 Attempts added: +{count}. Balance: {free}.",
        "btn_chat": "💬 AI Chat",
        "btn_image": "🎨 Images",
        "btn_video": "🎬 Video",
        "btn_oferta": "📖 Terms",
        "btn_exit_chat": "↩️ Exit chat",
        "oferta_accept_btn": "✅ I accept the terms",
        "oferta_required": (
            "⚠️ To use the bot, please accept the terms of the offer first — "
            "tap the button below."
        ),
        "oferta_accepted_msg": "✅ Terms accepted! Welcome 🎉",
        "oferta_text": (
            "📜 PUBLIC OFFER TERMS\n\n"
            "1. The service is the VoicePlan AI assistant: AI chat, image generation, "
            "task planner and calendar with reminders.\n"
            "2. Free tier: new users get daily chat and image quotas plus trial "
            "voice replies; quotas refresh once a day. Planner, tasks and "
            "calendar are unlimited.\n"
            "3. Paid features (chat, images, voice replies) are paid from your "
            "balance via Telegram Stars in “💎 Balance & pay”. No auto-renewal — "
            "you top up when you want.\n"
            "4. Refunds — contact support: an unused balance is refunded in full; "
            "during service outages please contact support.\n"
            "5. AI answers are for information only and are not professional advice.\n"
            "6. Data (name, ID, request texts) is used solely to operate the service "
            "and is passed to its providers (Telegram, AI API). It is never sold to "
            "third-party advertisers.\n"
            "7. Prohibited: illegal activity, spam, circumventing limits. Access may "
            "be restricted for violations.\n"
            "8. The service is provided “as is”; downtime may occur.\n"
            "9. Support: {contact}.\n\n"
            "By tapping “I accept the terms” you accept this public offer "
            "(Art. 437 of the Civil Code) and start using the service."
        ),
        "chat_started": (
            "💬 AI chat is on — ask anything.\n"
            "To leave: tap “↩️ Exit chat” or any menu button."
        ),
        "chat_exited": "✅ Chat closed. Send your plans — I’ll split them into tasks and events.",
        "image_started": "🎨 Send a description of the image in one message — I’ll draw it.",
        "image_generating": "🎨 Generating an image…",
        "image_done": "🎨 Done! Free images left today: {left}.",
        "image_done_paid": "🎨 Done!",
        "tries_limit": (
            "⛔ Free attempts for today are used up.\n"
            "Check your balance — the button below. "
            "Free features refresh once a day."
        ),
        "bal_open": "💰 View balance",
        "balance_text": (
            "💰 Balance\n"
            "Total: {total} RUB\n"
            "Paid: {paid} RUB\n"
            "Bonus: {bonus} RUB\n\n"
            "🆓 Free today:\n"
            "• Chat: {chats} of {chats_max}\n"
            "• Images: {images} of {images_max}\n"
            "• Trial voice replies: {tts_free}\n\n"
            "Free features refresh once a day.\n"
            "Prices: chat {price_chat} RUB, image {price_image} RUB, "
            "voice {price_tts} RUB/1000 characters.\n"
            "Planner, tasks and calendar — free forever."
        ),
        "bal_topup": "💰 Top up balance",
        "bal_free_gift": "🎁 Get balance for free",
        "bal_how": "ℹ️ How the balance works",
        "bal_save": "💡 How to save balance",
        "bal_spends_on": "✅ Show charges — turn off",
        "bal_spends_off": "🔕 Show charges — turn on",
        "bal_close": "❌ Close",
        "bal_back": "⬅️ Back",
        "popular": "🔥 POPULAR",
        "topup_title": (
            "💰 Top up your balance\n"
            "Choose a package (paid via Telegram Stars):"
        ),
        "topup_done": (
            "✅ {amount} RUB added to your balance (+{bonus} RUB bonus).\n"
            "Total: {total} RUB"
        ),
        "free_gift_text": (
            "🎁 Free:\n"
            "• Every day: {chats} chats and {images} image — "
            "refreshes once a day.\n"
            "• New users get {tts} trial voice replies.\n"
            "• Planner, tasks and calendar — always free."
        ),
        "how_balance_text": (
            "ℹ️ How the balance works:\n"
            "• Free features refresh once a day.\n"
            "• Planner — free forever."
        ),
        "save_balance_text": (
            "💡 How to save your balance:\n"
            "• Use the daily free quota: {chats} chats and {images} "
            "image every day.\n"
            "• Keep questions short and to the point.\n"
            "• Turn on voice replies only when you need them.\n"
            "• Keep tasks and plans in the planner — it is free."
        ),
        "spends_on": "✅ Charges are now shown",
        "spends_off": "🔕 Charges will no longer be shown",
        "spend_receipt": "💳 Charged {amount} RUB ({what}). Balance left: {total} RUB.",
        "spend_chat": "chat",
        "spend_image": "image",
        "spend_tts": "voice reply",
        "tts_locked": (
            "🔇 Trial voice replies are used up. Voice replies will turn on "
            "with a positive balance ({price} RUB/1000 characters)."
        ),
        "video_stub": "🎬 Video generation is coming very soon!",
        "yookassa_stub": "💳 YooKassa payments are not connected yet. Coming soon!",
        "btn_planner": "📋 Tasks & calendar",
        "nav_folders": "📂 Folders",
        "nav_calendar": "🗓 Calendar",
        "nav_reminders": "⏰ Reminders",
        "nav_events": "📅 Events",
        "nav_newcat": "➕ New folder",
        "nav_tasks": "⬅️ To tasks",
        "report_usage": "Report format:\n/report 01.10.2026 10:00 05.10.2026 23:59\nor without time: /report 01.10 05.10",
    },
}

REMINDER_CHOICES = {
    "15": 15,
    "30": 30,
    "60": 60,
    "1440": 1440,  # остаётся валидным для уже сохранённых событий и снятия
}

# Быстрые кнопки напоминаний (бесплатный выбор): 15 минут, 30 минут, 1 час.
REMINDER_QUICK = ("15", "30", "60")

# Короткие подписи для кнопок под сообщением «выберите время напоминания».
REMINDER_SHORT = {
    "ru": {"15": "15 минут", "30": "30 минут", "60": "1 час"},
    "en": {"15": "15 min", "30": "30 min", "60": "1 hour"},
}

REMINDER_LABELS = {
    "ru": {
        "10": "за 10 минут",  # легаси-значение до миграции на 15 минут
        "15": "за 15 минут",
        "30": "за 30 минут",
        "60": "за 1 час",
        "1440": "за 1 день",
    },
    "en": {
        "10": "10 minutes before",
        "15": "15 minutes before",
        "30": "30 minutes before",
        "60": "1 hour before",
        "1440": "1 day before",
    },
}

SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@your_support_username")


def tr(uid, key, **kwargs):
    lang = get_user_lang(uid)
    # Безопасный fallback: своя языковая таблица -> русская -> сам ключ.
    # (Старый вариант .get(key, LANGS["ru"][key]) падал с KeyError,
    #  потому что значение по умолчанию вычисляется заранее.)
    table = LANGS.get(lang) or LANGS["ru"]
    template = table.get(key) or LANGS["ru"].get(key) or str(key)
    return template.format(**kwargs)


# ============================================================
# 3. SQLITE И МИГРАЦИИ
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def table_columns(conn, table_name):
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def init_db():
    with db_lock:
        conn = get_db()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    language TEXT DEFAULT 'ru',
                    free_voice_left INTEGER DEFAULT 5,
                    is_premium INTEGER DEFAULT 0,
                    timezone TEXT DEFAULT 'Europe/Moscow',
                    reminder_offsets TEXT DEFAULT '[]',
                    voice_replies INTEGER DEFAULT 0,
                    balance_paid REAL DEFAULT 0,
                    balance_bonus REAL DEFAULT 0,
                    tts_free_left INTEGER DEFAULT 3,
                    free_day TEXT DEFAULT '',
                    daily_chat_left INTEGER DEFAULT 3,
                    daily_image_left INTEGER DEFAULT 1,
                    show_spends INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Миграции существующей базы без смены её имени.
            cols = table_columns(conn, "users")
            migrations = {
                "timezone": "TEXT DEFAULT 'Europe/Moscow'",
                "reminder_offsets": "TEXT DEFAULT '[]'",
                "voice_replies": "INTEGER DEFAULT 0",
                "created_at": "TEXT",
                "oferta_accepted": "INTEGER DEFAULT 0",
                # Баланс ИИ: платная и бонусная часть, пробные озвучки,
                # дневные бесплатные нормы и квитанции о списании.
                "balance_paid": "REAL DEFAULT 0",
                "balance_bonus": "REAL DEFAULT 0",
                "tts_free_left": f"INTEGER DEFAULT {FREE_TTS_TRIES}",
                "free_day": "TEXT DEFAULT ''",
                "daily_chat_left": f"INTEGER DEFAULT {FREE_CHATS_PER_DAY}",
                "daily_image_left": f"INTEGER DEFAULT {FREE_IMAGES_PER_DAY}",
                "show_spends": "INTEGER DEFAULT 1",
            }
            for column, definition in migrations.items():
                if column not in cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS custom_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category_name TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    is_completed INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    start_utc TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    reminders_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    cancelled INTEGER DEFAULT 0
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    due_utc TEXT NOT NULL,
                    sent INTEGER DEFAULT 0,
                    UNIQUE(event_id, due_utc)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_stars INTEGER DEFAULT 0,
                    amount_rub REAL DEFAULT 0,
                    payment_system TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Миграция старой payments-таблицы.
            pcols = table_columns(conn, "payments")
            if "amount_stars" not in pcols:
                conn.execute("ALTER TABLE payments ADD COLUMN amount_stars INTEGER DEFAULT 0")
            if "amount_rub" not in pcols:
                conn.execute("ALTER TABLE payments ADD COLUMN amount_rub REAL DEFAULT 0")
            if "payment_system" not in pcols:
                conn.execute("ALTER TABLE payments ADD COLUMN payment_system TEXT")
            if "created_at" not in pcols:
                conn.execute("ALTER TABLE payments ADD COLUMN created_at TEXT")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_user_start ON events(user_id, start_utc)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, due_utc)"
            )

            # Статистика для админ-отчёта: активность и запросы к ИИ.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT DEFAULT 'text',
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    audio_seconds REAL DEFAULT 0,
                    cost_rub REAL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_requests_ts ON ai_requests(created_at)"
            )
            migrate_reminder_10_to_15(conn)
            conn.commit()
        finally:
            conn.close()


def migrate_reminder_10_to_15(conn):
    """Идемпотентная миграция: интервал «за 10 минут» заменён на «за 15 минут».

    Меняет настройки пользователей, сохранённые события и ещё не отправленные
    срабатывания (если новое время срабатывания ещё не прошло).
    """
    def convert(raw):
        try:
            values = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return None
        if not any(str(x) == "10" for x in values):
            return None
        result = []
        for x in values:
            item = 15 if str(x) == "10" else int(x)
            if item not in result:
                result.append(item)
        result.sort(reverse=True)
        return json.dumps(result)

    for row in conn.execute("SELECT user_id, reminder_offsets FROM users").fetchall():
        new_value = convert(row["reminder_offsets"])
        if new_value is not None:
            conn.execute(
                "UPDATE users SET reminder_offsets=? WHERE user_id=?",
                (new_value, row["user_id"]),
            )

    now = datetime.now(timezone.utc)
    for row in conn.execute("SELECT event_id, start_utc, reminders_json FROM events").fetchall():
        new_value = convert(row["reminders_json"])
        if new_value is None:
            continue
        conn.execute(
            "UPDATE events SET reminders_json=? WHERE event_id=?",
            (new_value, row["event_id"]),
        )
        try:
            start = datetime.fromisoformat(row["start_utc"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        old_due = (start - timedelta(minutes=10)).isoformat(timespec="seconds")
        new_due_dt = start - timedelta(minutes=15)
        if new_due_dt <= now:
            continue
        conn.execute(
            "UPDATE OR IGNORE reminders SET due_utc=? "
            "WHERE event_id=? AND sent=0 AND due_utc=?",
            (new_due_dt.isoformat(timespec="seconds"), row["event_id"], old_due),
        )


init_db()


def get_user_lang(uid):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT language FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            return row["language"] if row and row["language"] in LANGS else "ru"
        finally:
            conn.close()


def get_user_data(uid):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT free_voice_left, is_premium FROM users WHERE user_id = ?",
                (uid,),
            ).fetchone()
            return (row["free_voice_left"], row["is_premium"]) if row else (5, 0)
        finally:
            conn.close()


def oferta_ok(uid):
    """Принял ли пользователь условия оферты (админ — всегда «да»)."""
    if uid == ADMIN_ID:
        return True
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT oferta_accepted FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            return bool(row and row["oferta_accepted"])
        finally:
            conn.close()


def send_oferta_prompt(chat_id, uid):
    """Отправляет текст оферты с кнопкой принятия."""
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "oferta_accept_btn"), callback_data="oferta:accept"
        )
    )
    bot.send_message(
        chat_id,
        tr(uid, "oferta_required") + "\n\n" + tr(uid, "oferta_text", contact=SUPPORT_CONTACT),
        reply_markup=markup,
    )


def oferta_gate(message):
    """True — можно работать; иначе отправляет оферту и блокирует действие."""
    uid = message.from_user.id
    if oferta_ok(uid):
        return True
    send_oferta_prompt(message.chat.id, uid)
    return False


def consume_attempt(uid):
    """Списать одну бесплатную попытку (легаси: голосовые входы планера)."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE users SET free_voice_left = MAX(free_voice_left - 1, 0) WHERE user_id=?",
                (uid,),
            )
            conn.commit()
        finally:
            conn.close()


# ------------------------------------------------------------
# БАЛАНС ИИ: дневные бесплатные нормы, платный/бонусный баланс.
# ------------------------------------------------------------

def ensure_daily(uid):
    """Сброс дневных бесплатных норм раз в сутки (по поясу пользователя)."""
    today = _now_in_user_tz(uid).date().isoformat()
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            row = conn.execute(
                "SELECT free_day FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if not row or (row["free_day"] or "") != today:
                conn.execute(
                    """UPDATE users SET free_day = ?, daily_chat_left = ?,
                       daily_image_left = ? WHERE user_id = ?""",
                    (today, FREE_CHATS_PER_DAY, FREE_IMAGES_PER_DAY, uid),
                )
                conn.commit()
        finally:
            conn.close()


def get_balance(uid):
    """(paid, bonus, tts_free, chat_left, image_left, show_spends) с суточным сбросом."""
    ensure_daily(uid)
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                """SELECT balance_paid, balance_bonus, tts_free_left,
                          daily_chat_left, daily_image_left, show_spends
                   FROM users WHERE user_id = ?""",
                (uid,),
            ).fetchone()
            if not row:
                return (
                    0.0, 0.0, FREE_TTS_TRIES,
                    FREE_CHATS_PER_DAY, FREE_IMAGES_PER_DAY, 1,
                )
            return (
                float(row["balance_paid"] or 0),
                float(row["balance_bonus"] or 0),
                int(row["tts_free_left"] or 0),
                int(row["daily_chat_left"] or 0),
                int(row["daily_image_left"] or 0),
                int(row["show_spends"] if row["show_spends"] is not None else 1),
            )
        finally:
            conn.close()


def consume_daily(uid, kind):
    """Списать одну дневную бесплатную норму: 'chat' или 'image'."""
    column = "daily_chat_left" if kind == "chat" else "daily_image_left"
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                f"UPDATE users SET {column} = MAX({column} - 1, 0) WHERE user_id = ?",
                (uid,),
            )
            conn.commit()
        finally:
            conn.close()


def consume_tts_trial(uid):
    """Списать одну пробную озвучку (выдаётся один раз при подключении)."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE users SET tts_free_left = MAX(tts_free_left - 1, 0) WHERE user_id = ?",
                (uid,),
            )
            conn.commit()
        finally:
            conn.close()


def spend_from_balance(uid, amount):
    """Списать amount ₽: сначала бонусный, затем платный. True — успех."""
    amount = round(float(amount), 2)
    if amount <= 0:
        return True
    paid, bonus, *_rest = get_balance(uid)
    if round(paid + bonus, 2) + 1e-9 < amount:
        return False
    from_bonus = min(bonus, amount)
    from_paid = amount - from_bonus
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE users SET balance_bonus = ?, balance_paid = ? WHERE user_id = ?",
                (round(bonus - from_bonus, 2), round(paid - from_paid, 2), uid),
            )
            conn.commit()
        finally:
            conn.close()
    return True


def has_paid_balance(uid):
    """Есть ли у пользователя положительный баланс (платный или бонусный)."""
    paid, bonus, *_rest = get_balance(uid)
    return round(float(paid) + float(bonus), 2) > 0


def maybe_receipt(uid, amount, what_key):
    """Квитанция о списании — если пользователь не выключил «Показывать списания»."""
    paid, bonus, _tts, _chat, _img, show = get_balance(uid)
    if not show:
        return
    try:
        bot.send_message(
            uid,
            tr(
                uid,
                "spend_receipt",
                amount=f"{amount:.2f}",
                what=tr(uid, what_key),
                total=f"{paid + bonus:.2f}",
            ),
        )
    except Exception:
        logger.exception("Не удалось отправить квитанцию о списании")


def get_user_preferences(uid):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT timezone, reminder_offsets, voice_replies FROM users WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if not row:
                return "Europe/Moscow", [], 0
            try:
                offsets = json.loads(row["reminder_offsets"] or "[]")
            except json.JSONDecodeError:
                offsets = []
            return row["timezone"] or "Europe/Moscow", offsets, row["voice_replies"]
        finally:
            conn.close()


def get_user_categories(uid):
    lang = get_user_lang(uid)
    categories = list(SYSTEM_CATEGORIES[lang])
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT category_name FROM custom_categories WHERE user_id = ? ORDER BY id",
                (uid,),
            ).fetchall()
            categories.extend(row["category_name"] for row in rows)
        finally:
            conn.close()
    return categories


def register_user(message):
    uid = message.from_user.id
    detected = "en" if (message.from_user.language_code or "").startswith("en") else "ru"
    username = message.from_user.username or ""
    full_name = message.from_user.first_name or "User"

    with db_lock:
        conn = get_db()
        try:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                    (username, full_name, uid),
                )
                is_new = False
            else:
                conn.execute(
                    "INSERT INTO users (user_id, username, full_name, language) VALUES (?, ?, ?, ?)",
                    (uid, username, full_name, detected),
                )
                is_new = True
            conn.commit()
        finally:
            conn.close()

    if is_new and ADMIN_ID:
        try:
            bot.send_message(
                ADMIN_ID,
                f"👤 Новая регистрация\nИмя: {full_name}\n"
                f"Username: @{username or 'нет'}\nID: {uid}\nЯзык: {detected}",
            )
        except Exception:
            logger.exception("Не удалось уведомить администратора о регистрации")


def log_activity(uid):
    """Отметка активности пользователя (для отчёта «сколько в сети»)."""
    try:
        with db_lock:
            conn = get_db()
            try:
                conn.execute("INSERT INTO activity (user_id) VALUES (?)", (uid,))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception("Не удалось записать активность пользователя")


def log_ai_request(uid, kind, prompt_tokens=0, completion_tokens=0, audio_seconds=0, cost_rub=None):
    """Запрос к ИИ + его стоимость в рублях (для отчёта).

    cost_rub — задать напрямую, если у запроса фиксированная цена
    (например, генерация картинки), а не расчёт по токенам.
    """
    if cost_rub is not None:
        cost = float(cost_rub)
    else:
        cost = (float(prompt_tokens) / 1_000_000) * LLM_PRICE_IN_RUB
        cost += (float(completion_tokens) / 1_000_000) * LLM_PRICE_OUT_RUB
        if audio_seconds:
            cost += (float(audio_seconds) / 60.0) * WHISPER_PRICE_RUB_PER_MIN
    try:
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    """INSERT INTO ai_requests
                       (user_id, kind, prompt_tokens, completion_tokens, audio_seconds, cost_rub)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        uid,
                        kind,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        float(audio_seconds or 0),
                        round(cost, 4),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception("Не удалось записать запрос ИИ")


# ============================================================
# 4. КЛАВИАТУРЫ И СТАРТОВОЕ МЕНЮ
# ============================================================

def balance_open_markup(uid):
    """Кнопка «Посмотреть баланс» под сообщением об исчерпании лимитов."""
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(tr(uid, "bal_open"), callback_data="bal:show")
    )
    return markup


def balance_screen(uid):
    """Экран «💰 Баланс»: таблица + кнопки (по образцу скриншота)."""
    paid, bonus, tts_free, chat_left, image_left, show = get_balance(uid)
    text = tr(
        uid,
        "balance_text",
        total=f"{paid + bonus:.2f}",
        paid=f"{paid:.2f}",
        bonus=f"{bonus:.2f}",
        chats=chat_left,
        chats_max=FREE_CHATS_PER_DAY,
        images=image_left,
        images_max=FREE_IMAGES_PER_DAY,
        tts_free=tts_free,
        price_chat=f"{PRICE_CHAT_RUB:g}",
        price_image=f"{PRICE_IMAGE_RUB:g}",
        price_tts=f"{PRICE_TTS_RUB_PER_1K:g}",
    )
    spends_label = tr(uid, "bal_spends_on") if show else tr(uid, "bal_spends_off")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"),
        types.InlineKeyboardButton(tr(uid, "bal_free_gift"), callback_data="bal:free"),
        types.InlineKeyboardButton(tr(uid, "bal_how"), callback_data="bal:how"),
        types.InlineKeyboardButton(tr(uid, "bal_save"), callback_data="bal:save"),
        types.InlineKeyboardButton(spends_label, callback_data="bal:spends"),
        types.InlineKeyboardButton(tr(uid, "bal_close"), callback_data="bal:close"),
    )
    return text, markup


def topup_screen(uid):
    """Экран «💰 Пополнить баланс»: пакеты 199/490/990 ₽ через Telegram Stars."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    for rub, bonus_pct in TOPUP_PACKAGES:
        bonus_rub = rub * bonus_pct // 100
        # XTR считаем 1:1 к рублю (формат прежнего меню); курс Stars
        # можно уточнить позже, изменив только этот цикл.
        label = f"⭐ {rub} XTR — {rub} ₽"
        if bonus_rub:
            label += f" (+{bonus_rub} ₽ бонус)"
        if rub == 490:
            label += f" {tr(uid, 'popular')}"
        markup.add(
            types.InlineKeyboardButton(label, callback_data=f"bal:buy:{rub}")
        )
    markup.add(
        types.InlineKeyboardButton(tr(uid, "bal_back"), callback_data="bal:show")
    )
    markup.add(
        types.InlineKeyboardButton("💳 ЮKassa", callback_data="pay_yookassa")
    )
    return tr(uid, "topup_title"), markup


def main_keyboard(uid):
    """Главное меню: ИИ-функции сверху, планер одной кнопкой, служебные снизу."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton(tr(uid, "btn_chat")),
        types.KeyboardButton(tr(uid, "btn_image")),
    )
    markup.add(
        types.KeyboardButton(tr(uid, "btn_video")),
        types.KeyboardButton(tr(uid, "btn_premium")),
    )
    markup.add(types.KeyboardButton(tr(uid, "btn_planner")))
    markup.add(
        types.KeyboardButton(tr(uid, "btn_support")),
        types.KeyboardButton(tr(uid, "btn_oferta")),
    )
    markup.add(types.KeyboardButton(tr(uid, "btn_language")))
    return markup


def language_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
    )
    return markup


@bot.message_handler(commands=["start"])
def handle_start(message):
    register_user(message)
    user_states.pop(message.from_user.id, None)

    # Первый контакт: показываем публичную оферту, пока она не принята.
    if not oferta_ok(message.from_user.id):
        send_oferta_prompt(message.chat.id, message.from_user.id)
        return

    _paid, _bonus, _tts, chats, images, _show = get_balance(message.from_user.id)
    bot.send_message(
        message.chat.id,
        tr(
            message.from_user.id,
            "welcome",
            name=message.from_user.first_name or "друг",
            chats=chats,
            images=images,
            total=f"{_paid + _bonus:.2f}",
        ),
        reply_markup=main_keyboard(message.from_user.id),
    )


@bot.message_handler(commands=["help"])
def handle_help(message):
    uid = message.from_user.id
    if get_user_lang(uid) == "en":
        text = (
            "Send a voice note or text with your plans. "
            "I’ll sort tasks and events. Use the menu to view tasks, events, folders, "
            "calendar, settings, or change language. "
            "Commands: /calendar — events calendar, /balance — AI balance."
        )
    else:
        text = (
            "Отправьте голосовое или текст с планами. Я разделю задачи и события. "
            "Используйте меню, чтобы просматривать дела, события, папки, календарь и настройки. "
            "Команды: /calendar — календарь событий, /balance — баланс ИИ."
        )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(uid))


# ============================================================
# 5. ЗАДАЧИ, СОБЫТИЯ И ПАПКИ
# ============================================================

def _planner_nav(uid, markup):
    """Навигация раздела «Задачи и календарь» (единая кнопка в главном меню)."""
    _tz, _offsets, voice_on = get_user_preferences(uid)
    voice_label = tr(uid, "voice_on") if voice_on else tr(uid, "voice_off")
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_folders"), callback_data="planner:folders"),
        types.InlineKeyboardButton(tr(uid, "nav_calendar"), callback_data="planner:calendar"),
    )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_reminders"), callback_data="setting:reminders"),
        types.InlineKeyboardButton(voice_label, callback_data="setting:voice"),
    )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_events"), callback_data="planner:events"),
        types.InlineKeyboardButton(tr(uid, "nav_newcat"), callback_data="planner:newcat"),
    )
    return markup


def dashboard(uid):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT task_id, category, task_text, is_completed
                   FROM tasks WHERE user_id = ?
                   ORDER BY is_completed ASC, category ASC, task_id DESC""",
                (uid,),
            ).fetchall()
        finally:
            conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)
    if not rows:
        _planner_nav(uid, markup)
        return tr(uid, "empty_tasks"), markup

    lines = [tr(uid, "dash_title")]
    current_category = None

    for row in rows:
        category = row["category"] or SYSTEM_CATEGORIES[get_user_lang(uid)][-1]
        if category != current_category:
            current_category = category
            lines.append(f"\n📂 {category}")

        done = bool(row["is_completed"])
        # Активные задачи показываем только красным кружком, без слов «В работе».
        lines.append(f"{'🟢' if done else '🔴'} {('~' + row['task_text'] + '~') if done else row['task_text']}")
        label = tr(uid, "btn_undone" if done else "btn_done")
        markup.add(
            types.InlineKeyboardButton(
                f"{label}: {row['task_text'][:25]}",
                callback_data=f"task:{row['task_id']}",
            )
        )

    _planner_nav(uid, markup)
    return "\n".join(lines), markup


@bot.message_handler(commands=["tasks"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_tasks"], LANGS["en"]["btn_tasks"]
})
def show_tasks(message):
    text, markup = dashboard(message.from_user.id)
    bot.send_message(message.chat.id, text, reply_markup=markup)


def upcoming_events_text(uid):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT event_id, title, start_utc, timezone, reminders_json
                   FROM events
                   WHERE user_id = ? AND cancelled = 0 AND start_utc >= ?
                   ORDER BY start_utc LIMIT 20""",
                (uid, now),
            ).fetchall()
        finally:
            conn.close()

    if not rows:
        return tr(uid, "empty_events"), None

    lines = []
    markup = types.InlineKeyboardMarkup(row_width=1)
    user_lang = get_user_lang(uid)

    for row in rows:
        try:
            local_dt = datetime.fromisoformat(row["start_utc"]).astimezone(
                ZoneInfo(row["timezone"])
            )
            date_text = local_dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            date_text = row["start_utc"]

        try:
            offsets = json.loads(row["reminders_json"] or "[]")
        except json.JSONDecodeError:
            offsets = []

        reminder_text = format_reminder_labels(uid, offsets)
        lines.append(
            tr(uid, "event_card", title=row["title"], date=date_text, reminders=reminder_text)
        )
        markup.add(
            types.InlineKeyboardButton(
                f"🗑 {row['title'][:30]}",
                callback_data=f"eventdel:{row['event_id']}",
            )
        )

    return "\n\n".join(lines), markup


@bot.message_handler(commands=["events"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_events"], LANGS["en"]["btn_events"]
})
def show_events(message):
    text, markup = upcoming_events_text(message.from_user.id)
    bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["folders"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_folders"], LANGS["en"]["btn_folders"]
})
def folders_view(uid):
    """Список папок с задачами + кнопки навигации (общая для сообщения и callback)."""
    categories = get_user_categories(uid)
    lines = []
    found = False
    filled = []  # (индекс в categories, название) — для кнопок просмотра папки

    with db_lock:
        conn = get_db()
        try:
            for index, category in enumerate(categories):
                rows = conn.execute(
                    """SELECT task_text, is_completed FROM tasks
                       WHERE user_id = ? AND category = ?
                       ORDER BY is_completed ASC, task_id DESC""",
                    (uid, category),
                ).fetchall()

                if not rows:
                    continue

                found = True
                filled.append((index, category))
                lines.append(f"📁 {category}")
                for row in rows:
                    done = bool(row["is_completed"])
                    task = f"~{row['task_text']}~" if done else row["task_text"]
                    lines.append(f"{'🟢' if done else '🔴'} {task}")
                lines.append("")
        finally:
            conn.close()

    text = "\n".join(lines) if found else tr(uid, "folders_empty")

    markup = types.InlineKeyboardMarkup(row_width=2)
    if filled:
        buttons = [
            types.InlineKeyboardButton(
                f"📁 {category[:20]}", callback_data=f"folder:{index}"
            )
            for index, category in filled
        ]
        for i in range(0, len(buttons), 2):
            markup.row(*buttons[i:i + 2])
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_tasks"), callback_data="planner:tasks")
    )
    return text, markup


def show_folders(message):
    text, markup = folders_view(message.from_user.id)
    bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("task:"))
def toggle_task(call):
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid task")
        return

    uid = call.from_user.id
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT is_completed FROM tasks WHERE task_id=? AND user_id=?",
                (task_id, uid),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE tasks SET is_completed=? WHERE task_id=? AND user_id=?",
                    (0 if row["is_completed"] else 1, task_id, uid),
                )
                conn.commit()
        finally:
            conn.close()

    bot.answer_callback_query(call.id, "✅")
    text, markup = dashboard(uid)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
    except Exception:
        logger.exception("Не удалось обновить карточку задач")


@bot.callback_query_handler(func=lambda call: call.data.startswith("eventdel:"))
def delete_event(call):
    try:
        event_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid event")
        return

    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE events SET cancelled=1 WHERE event_id=? AND user_id=?",
                (event_id, call.from_user.id),
            )
            conn.execute(
                "UPDATE reminders SET sent=1 WHERE event_id=? AND user_id=?",
                (event_id, call.from_user.id),
            )
            conn.commit()
        finally:
            conn.close()

    bot.answer_callback_query(call.id, "🗑")
    text, markup = upcoming_events_text(call.from_user.id)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
    except Exception:
        pass


# ============================================================
# 5.1 ПРОСМОТР ОДНОЙ ПАПКИ
# ============================================================

def folder_tasks_text(uid, category):
    """Список задач одной папки."""
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT task_text, is_completed FROM tasks
                   WHERE user_id = ? AND category = ?
                   ORDER BY is_completed ASC, task_id DESC""",
                (uid, category),
            ).fetchall()
        finally:
            conn.close()

    if not rows:
        return f"📁 {category}\n" + tr(uid, "folders_empty")

    lines = [f"📁 {category}"]
    for row in rows:
        done = bool(row["is_completed"])
        task = f"~{row['task_text']}~" if done else row["task_text"]
        lines.append(f"{'🟢' if done else '🔴'} {task}")
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda call: call.data.startswith("folder:"))
def open_folder(call):
    uid = call.from_user.id
    try:
        index = int(call.data.split(":", 1)[1])
        category = get_user_categories(uid)[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid folder")
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, folder_tasks_text(uid, category))


# ============================================================
# 5.2 КАЛЕНДАРЬ СОБЫТИЙ
# ============================================================

MONTHS = {
    "ru": [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ],
    "en": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
}
WEEKDAYS_HEADER = {"ru": "Пн Вт Ср Чт Пт Сб Вс", "en": "Mo Tu We Th Fr Sa Su"}


def format_reminder_labels(uid, offsets):
    """Список интервалов в виде «за 15 минут, за 1 час, за 1 день» (по возрастанию)."""
    labels = REMINDER_LABELS[get_user_lang(uid)]
    values = []
    for x in offsets:
        try:
            values.append(int(x))
        except (TypeError, ValueError):
            continue
    values = sorted(set(values))
    text = ", ".join(labels.get(str(v), f"{v} мин") for v in values)
    return text or tr(uid, "reminders_none")


def parse_custom_reminder(text):
    """Разбор произвольного времени напоминания из текста → минуты (int) или None.

    Примеры: «30 мин», «45 минут», «2 часа», «1 день», «90 м», «полтора часа».
    Допустимый диапазон: 1 минута … 7 суток (10080 мин).
    """
    import re

    raw = (text or "").strip().lower().replace("ё", "е")
    if not raw:
        return None

    # Особый случай: «полтора часа» = 90 минут.
    if "полтора" in raw and "час" in raw:
        return 90

    match = re.search(r"(\d+)\s*(мин|м|хв|минут|hour|hours|час|ч|day|days|день|дней|д|сутки|суток)", raw)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    if unit in ("час", "ч", "hour", "hours"):
        minutes = value * 60
    elif unit in ("день", "дней", "д", "day", "days", "сутки", "суток"):
        minutes = value * 1440
    else:  # минуты
        minutes = value

    if 1 <= minutes <= 7 * 1440:
        return minutes
    return None


def set_event_reminder(uid, event_id, minutes):
    """Установить одно напоминание к событию (перезаписывает reminders_json)."""
    minutes = int(minutes)
    now = datetime.now(timezone.utc)
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT start_utc, cancelled FROM events WHERE event_id=? AND user_id=?",
                (event_id, uid),
            ).fetchone()
            if not row or row["cancelled"]:
                return None
            start = datetime.fromisoformat(row["start_utc"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            due = start - timedelta(minutes=minutes)
            # Напоминание не позже самого события.
            if due < now:
                due = now
            due_text = due.isoformat(timespec="seconds")
            conn.execute(
                "UPDATE events SET reminders_json=? WHERE event_id=? AND user_id=?",
                (json.dumps([minutes]), event_id, uid),
            )
            conn.execute("DELETE FROM reminders WHERE event_id=?", (event_id,))
            conn.execute(
                "INSERT OR IGNORE INTO reminders (event_id, user_id, due_utc, sent) VALUES (?, ?, ?, 0)",
                (event_id, uid, due_text),
            )
            conn.commit()
            return minutes
        finally:
            conn.close()


def _events_by_local_date(uid):
    """Словарь {локальная дата: [(название, 'HH:MM', [datetime срабатываний]), ...]}."""
    user_tz_name, _off, _voice = get_user_preferences(uid)
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT title, start_utc, timezone, reminders_json FROM events "
                "WHERE user_id = ? AND cancelled = 0",
                (uid,),
            ).fetchall()
        finally:
            conn.close()

    result = {}
    for row in rows:
        try:
            tz = ZoneInfo(row["timezone"] or user_tz_name)
            local = datetime.fromisoformat(row["start_utc"]).astimezone(tz)
        except (ValueError, ZoneInfoNotFoundError):
            continue
        try:
            offsets = sorted({int(x) for x in json.loads(row["reminders_json"] or "[]")})
        except (ValueError, TypeError, json.JSONDecodeError):
            offsets = []
        fire_times = [local - timedelta(minutes=m) for m in offsets]
        result.setdefault(local.date(), []).append(
            (row["title"], local.strftime("%H:%M"), fire_times)
        )

    for items in result.values():
        items.sort(key=lambda item: item[1])
    return result


def _now_in_user_tz(uid):
    tz_name, _off, _voice = get_user_preferences(uid)
    try:
        return datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc)


def build_calendar(uid, year, month):
    """Текст месяца + кнопки: навигация и дни со событиями (дни отмечены '*')."""
    lang = get_user_lang(uid)
    events = _events_by_local_date(uid)
    month_name = MONTHS[lang][month - 1]
    weeks = calmod.Calendar(firstweekday=0).monthdayscalendar(year, month)

    lines = [tr(uid, "cal_title", month=f"{month_name} {year}"), ""]
    lines.append(WEEKDAYS_HEADER[lang])
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("    ")
            else:
                marker = "*" if date(year, month, day) in events else " "
                cells.append(f"{day:>3}{marker}")
        lines.append("".join(cells).rstrip())
    lines.append("")

    days_with = sorted(d for d in events if d.year == year and d.month == month)
    if days_with:
        lines.append(tr(uid, "cal_legend"))
        lines.append(", ".join(str(d.day) for d in days_with))
    else:
        lines.append(tr(uid, "cal_no_dates"))

    # Строка с выбранными интервалами напоминаний (все выбранные видны сразу).
    _tz, selected_offsets, _voice = get_user_preferences(uid)
    lines.append("")
    lines.append(tr(uid, "reminders", value=format_reminder_labels(uid, selected_offsets)))

    markup = types.InlineKeyboardMarkup(row_width=7)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    markup.row(
        types.InlineKeyboardButton("⬅️", callback_data=f"calnav:{prev_y:04d}-{prev_m:02d}"),
        types.InlineKeyboardButton(tr(uid, "cal_today"), callback_data="caltoday"),
        types.InlineKeyboardButton("➡️", callback_data=f"calnav:{next_y:04d}-{next_m:02d}"),
    )
    row_buttons = []
    for d in days_with:
        row_buttons.append(
            types.InlineKeyboardButton(str(d.day), callback_data=f"calday:{d.isoformat()}")
        )
        if len(row_buttons) == 7:
            markup.row(*row_buttons)
            row_buttons = []
    if row_buttons:
        markup.row(*row_buttons)

    # Интервалы напоминаний перенесены сюда из «Настроек» + возврат к задачам.
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_reminders"), callback_data="setting:reminders"),
        types.InlineKeyboardButton(tr(uid, "nav_tasks"), callback_data="planner:tasks"),
    )

    return "\n".join(lines), markup


def _send_or_edit_calendar(target, uid, year, month):
    """Отправить календарь сообщением или отредактировать существующее."""
    text, markup = build_calendar(uid, year, month)
    if isinstance(target, types.CallbackQuery):
        try:
            bot.edit_message_text(
                text,
                target.message.chat.id,
                target.message.message_id,
                reply_markup=markup,
            )
        except Exception:
            logger.exception("Не удалось обновить календарь")
        bot.answer_callback_query(target.id)
    else:
        bot.send_message(target.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["calendar"])
def calendar_command(message):
    now = _now_in_user_tz(message.from_user.id)
    _send_or_edit_calendar(message, message.from_user.id, now.year, now.month)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calnav:"))
def calendar_nav(call):
    uid = call.from_user.id
    try:
        year_s, month_s = call.data.split(":", 1)[1].split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid month")
        return
    if not 1 <= month <= 12:
        bot.answer_callback_query(call.id, "Invalid month")
        return
    _send_or_edit_calendar(call, uid, year, month)


@bot.callback_query_handler(func=lambda call: call.data == "caltoday")
def calendar_today(call):
    now = _now_in_user_tz(call.from_user.id)
    _send_or_edit_calendar(call, call.from_user.id, now.year, now.month)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calday:"))
def calendar_open_day(call):
    uid = call.from_user.id
    try:
        day = date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return

    items = _events_by_local_date(uid).get(day, [])
    if items:
        lines = [tr(uid, "cal_day_title", date=day.strftime("%d.%m.%Y"))]
        for title, time_hhmm, fire_times in items:
            lines.append(f"• {title} — {time_hhmm}")
            if fire_times:
                stamps = ", ".join(t.strftime("%d.%m %H:%M") for t in fire_times)
                lines.append(tr(uid, "cal_day_reminders", value=stamps))
    else:
        lines = [tr(uid, "cal_day_empty")]

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "\n".join(lines))


@bot.callback_query_handler(func=lambda call: call.data == "calnoop")
def calendar_noop(call):
    bot.answer_callback_query(call.id)


# ============================================================
# 6. ЯЗЫК И НАСТРОЙКИ
# ============================================================

@bot.message_handler(commands=["lang"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_language"], LANGS["en"]["btn_language"]
})
def choose_language(message):
    bot.send_message(
        message.chat.id,
        tr(message.from_user.id, "language_prompt"),
        reply_markup=language_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang:"))
def set_language(call):
    uid = call.from_user.id
    new_lang = call.data.split(":", 1)[1]
    if new_lang not in LANGS:
        bot.answer_callback_query(call.id, "Unknown language", show_alert=True)
        return

    with db_lock:
        conn = get_db()
        try:
            conn.execute("UPDATE users SET language=? WHERE user_id=?", (new_lang, uid))
            conn.commit()
        finally:
            conn.close()

    bot.answer_callback_query(call.id, "✅")
    _paid, _bonus, _tts, chats, images, _show = get_balance(uid)
    bot.send_message(
        call.message.chat.id,
        tr(uid, "language_saved"),
        reply_markup=main_keyboard(uid),
    )
    bot.send_message(
        call.message.chat.id,
        tr(
            uid,
            "welcome",
            name=call.from_user.first_name or "friend",
            chats=chats,
            images=images,
            total=f"{_paid + _bonus:.2f}",
        ),
    )


@bot.message_handler(commands=["settings"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_settings"], LANGS["en"]["btn_settings"]
})
def settings_menu(message):
    # Раздел «Настройки» убран из главного меню: интервалы напоминаний и озвучка
    # теперь в разделе «Задачи и календарь». Старые клавиатуры ведут сюда же.
    show_tasks(message)


@bot.callback_query_handler(func=lambda call: call.data == "setting:voice")
def toggle_voice_setting(call):
    uid = call.from_user.id
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT voice_replies FROM users WHERE user_id=?", (uid,)
            ).fetchone()
            value = 0 if row and row["voice_replies"] else 1
            conn.execute(
                "UPDATE users SET voice_replies=? WHERE user_id=?",
                (value, uid),
            )
            conn.commit()
        finally:
            conn.close()
    bot.answer_callback_query(call.id, "✅")
    # При включении озвучки без пробных попыток и баланса — предупреждение.
    if value == 1:
        paid, bonus, tts_free, _c, _i, _s = get_balance(uid)
        if tts_free <= 0 and paid + bonus <= 0:
            try:
                bot.send_message(
                    call.message.chat.id,
                    tr(uid, "tts_locked", price=f"{PRICE_TTS_RUB_PER_1K:g}"),
                )
            except Exception:
                logger.exception("Не удалось отправить подсказку об озвучке")
    text, markup = dashboard(uid)
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "setting:reminders")
def reminder_settings_menu(call):
    uid = call.from_user.id
    _tz, selected, _voice = get_user_preferences(uid)
    selected = {str(x) for x in selected}
    markup = types.InlineKeyboardMarkup(row_width=1)
    labels = REMINDER_LABELS[get_user_lang(uid)]
    for key in REMINDER_QUICK:
        marker = "✅ " if key in selected else ""
        markup.add(
            types.InlineKeyboardButton(
                marker + labels[key],
                callback_data=f"reminder_toggle:{key}",
            )
        )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_calendar"), callback_data="planner:calendar"),
        types.InlineKeyboardButton(tr(uid, "nav_tasks"), callback_data="planner:tasks"),
    )
    bot.answer_callback_query(call.id)
    text = tr(uid, "choose_reminders") + "\n\n" + tr(
        uid, "reminders", value=format_reminder_labels(uid, selected)
    )
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("reminder_toggle:"))
def toggle_reminder_preference(call):
    uid = call.from_user.id
    value = call.data.split(":", 1)[1]
    if value not in REMINDER_CHOICES:
        bot.answer_callback_query(call.id, "Invalid interval")
        return

    tz_name, offsets, _voice = get_user_preferences(uid)
    if value in {str(x) for x in offsets}:
        offsets = [x for x in offsets if str(x) != value]
    else:
        offsets.append(int(value))
        offsets.sort(reverse=True)

    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE users SET reminder_offsets=? WHERE user_id=?",
                (json.dumps(offsets), uid),
            )
            conn.commit()
        finally:
            conn.close()

    bot.answer_callback_query(call.id, "✅")
    reminder_settings_menu(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtrem:"))
def event_reminder_set(call):
    """Кнопка быстрого выбора времени напоминания для конкретного события."""
    uid = call.from_user.id
    try:
        _, event_id, value = call.data.split(":", 2)
        minutes = int(value)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid data")
        return
    if minutes not in REMINDER_CHOICES.values() and not (1 <= minutes <= 7 * 1440):
        bot.answer_callback_query(call.id, "Invalid interval")
        return
    result = set_event_reminder(uid, int(event_id), minutes)
    if not result:
        bot.answer_callback_query(call.id, "Event not found")
        return
    try:
        bot.edit_message_text(
            tr(uid, "remind_done", value=format_reminder_labels(uid, [minutes])),
            call.message.chat.id,
            call.message.message_id,
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            tr(uid, "remind_done", value=format_reminder_labels(uid, [minutes])),
        )
    bot.answer_callback_query(call.id, "✅")


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtremc:"))
def event_reminder_custom(call):
    """Кнопка «Своё время» — просим ввести время напоминания текстом."""
    uid = call.from_user.id
    event_id = call.data.split(":", 1)[1]
    user_states[uid] = f"custom_reminder:{event_id}"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, tr(uid, "remind_custom_prompt"))


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtrems:"))
def event_reminder_skip(call):
    """Кнопка «Без напоминания»."""
    uid = call.from_user.id
    bot.answer_callback_query(call.id, "✅")
    try:
        bot.edit_message_text(
            tr(uid, "remind_skipped"), call.message.chat.id, call.message.message_id
        )
    except Exception:
        bot.send_message(call.message.chat.id, tr(uid, "remind_skipped"))


# ============================================================
# 6-bis. РАЗДЕЛ «ЗАДАЧИ И КАЛЕНДАРЬ»: CALLBACK-НАВИГАЦИЯ
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("planner:"))
def planner_router(call):
    """Единый раздел: задачи, папки, календарь, события, новая папка."""
    uid = call.from_user.id
    action = call.data.split(":", 1)[1]

    def edit_or_send(text, markup):
        try:
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup)

    back_markup = types.InlineKeyboardMarkup()
    back_markup.add(
        types.InlineKeyboardButton(tr(uid, "nav_tasks"), callback_data="planner:tasks")
    )

    if action == "tasks":
        bot.answer_callback_query(call.id)
        text, markup = dashboard(uid)
        edit_or_send(text, markup)
    elif action == "calendar":
        now = _now_in_user_tz(uid)
        _send_or_edit_calendar(call, uid, now.year, now.month)
    elif action == "events":
        bot.answer_callback_query(call.id)
        text, markup = upcoming_events_text(uid)
        edit_or_send(text, markup or back_markup)
    elif action == "folders":
        bot.answer_callback_query(call.id)
        text, markup = folders_view(uid)
        edit_or_send(text, markup)
    elif action == "newcat":
        bot.answer_callback_query(call.id)
        user_states[uid] = "category"
        bot.send_message(call.message.chat.id, tr(uid, "category_prompt"))
    else:
        bot.answer_callback_query(call.id)


# ============================================================
# 7. ИИ: РАСПОЗНАВАНИЕ И СТРУКТУРИРОВАНИЕ
# ============================================================

def parse_ai_json(content):
    if isinstance(content, dict):
        return content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def transcribe_voice(file_path):
    if not AI_API_KEY:
        raise RuntimeError("AI_API_KEY is not configured")

    url = f"{AI_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    with open(file_path, "rb") as audio:
        response = requests.post(
            url,
            headers=headers,
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"},
            timeout=(15, 120),
        )

    if response.status_code != 200:
        logger.error("Transcription API %s: %s", response.status_code, response.text[:1000])
        raise RuntimeError("Transcription API error")

    return response.json().get("text", "").strip()


def get_open_tasks(uid):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT task_id, task_text FROM tasks WHERE user_id=? AND is_completed=0 ORDER BY task_id DESC LIMIT 100",
                (uid,),
            ).fetchall()
            return [{"id": row["task_id"], "text": row["task_text"]} for row in rows]
        finally:
            conn.close()


def analyze_user_text(uid, raw_text, kind="text", audio_seconds=0):
    if not AI_API_KEY:
        raise RuntimeError("AI_API_KEY is not configured")

    lang = get_user_lang(uid)
    categories = get_user_categories(uid)
    open_tasks = get_open_tasks(uid)

    if lang == "en":
        rules = """
Classify each new item into exactly one provided category:
- Calendar: events, appointments, calls, or tasks with a date/time.
- Groceries: items the user needs to buy.
- Urgent: explicitly urgent tasks due now/today.
- Ideas: thoughts and ideas without an immediate action.
- Inbox: other ordinary tasks.
Do not put every item in Calendar. A date can be stored as an event only if the user gives a clear date/time.
"""
        response_language = "English"
    else:
        rules = """
Распределяй каждую новую задачу ровно в одну из предоставленных категорий:
- 📅 Календарь — встречи, звонки и события с датой или временем.
- 🛒 Покупки — товары и продукты, которые нужно купить.
- 🔴 Срочно — только явно срочные дела, которые нужно сделать сейчас или сегодня.
- 💡 Идеи — мысли и идеи без конкретного ближайшего действия.
- 📥 Входящие — остальные обычные дела.
Не помещай все задачи в календарь. Событие создавай только при достаточно ясной дате или времени.
"""
        response_language = "Russian"

    prompt = f"""
You are a personal planner. Return only valid JSON.
Respond with task text in {response_language}.
Available categories: {json.dumps(categories, ensure_ascii=False)}
Open tasks: {json.dumps(open_tasks, ensure_ascii=False)}
{rules}

Extract:
1. completed_task_ids: IDs of existing tasks the user clearly says they completed.
2. new_tasks: short actionable items with exact category and text.
3. new_events: events with title, local_datetime in ISO format YYYY-MM-DDTHH:MM,
   or null if the date/time is unclear.
4. For each event, if the user explicitly asks to be reminded BEFORE it
   (e.g. "remind me 30 minutes before", "remind me 2 hours before",
   "remind me a day before"), set remind_minutes to that number of minutes
   (30, 120, 1440, ...). If no reminder time is stated, set remind_minutes to null.

If date is relative (e.g. tomorrow), resolve it using the current date:
{datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")}.

JSON schema:
{{
  "completed_task_ids": [],
  "new_tasks": [{{"category": "exact category", "text": "task"}}],
  "new_events": [{{"title": "event", "local_datetime": "2026-05-18T15:30", "remind_minutes": null}}]
}}
"""

    response = requests.post(
        f"{AI_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": raw_text},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        },
        timeout=(15, 60),
    )

    if response.status_code != 200:
        logger.error("Chat API %s: %s", response.status_code, response.text[:1000])
        raise RuntimeError("Chat API error")

    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    data = parse_ai_json(content)

    if not isinstance(data, dict):
        raise ValueError("AI response is not a JSON object")

    data.setdefault("completed_task_ids", [])
    data.setdefault("new_tasks", [])
    data.setdefault("new_events", [])

    # Учёт стоимости запроса для админ-отчёта.
    usage = payload.get("usage") or {}
    log_ai_request(
        uid,
        kind,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        audio_seconds=audio_seconds,
    )
    return data


# ============================================================
# 8. СОХРАНЕНИЕ ЗАДАЧ И СОБЫТИЙ
# ============================================================

def normalize_category(category, uid):
    allowed = get_user_categories(uid)
    if category in allowed:
        return category
    # Безопасный fallback: не теряем задачу из-за неточного ответа модели.
    return SYSTEM_CATEGORIES[get_user_lang(uid)][-1]


def create_event(uid, title, local_datetime, timezone_name, reminders):
    try:
        naive = datetime.fromisoformat(local_datetime)
        if naive.tzinfo is not None:
            local_dt = naive
        else:
            local_dt = naive.replace(tzinfo=ZoneInfo(timezone_name))
    except (ValueError, ZoneInfoNotFoundError):
        return None

    if local_dt <= datetime.now(ZoneInfo(timezone_name)):
        return None

    utc_dt = local_dt.astimezone(timezone.utc)
    utc_text = utc_dt.isoformat(timespec="seconds")

    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute(
                """INSERT INTO events
                   (user_id, title, start_utc, timezone, reminders_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid, title[:200], utc_text, timezone_name, json.dumps(reminders)),
            )
            event_id = cursor.lastrowid

            for minutes in reminders:
                due = utc_dt - timedelta(minutes=int(minutes))
                if due > datetime.now(timezone.utc):
                    conn.execute(
                        """INSERT OR IGNORE INTO reminders
                           (event_id, user_id, due_utc, sent)
                           VALUES (?, ?, ?, 0)""",
                        (event_id, uid, due.isoformat(timespec="seconds")),
                    )

            conn.commit()
            return event_id
        finally:
            conn.close()


def save_ai_result(uid, data, use_voice_limit=False):
    free, premium = get_user_data(uid)
    _paid, _bonus, _tts, _chat, _image, _show = get_balance(uid)
    has_paid_balance = (_paid + _bonus) > 0
    inserted_tasks = 0
    inserted_events = 0
    completed = 0
    no_reminder_events = []  # [(event_id, title)] — созданы без напоминания

    timezone_name, _default_reminders, _voice = get_user_preferences(uid)

    with db_lock:
        conn = get_db()
        try:
            for task_id in data.get("completed_task_ids", []):
                try:
                    cursor = conn.execute(
                        "UPDATE tasks SET is_completed=1 WHERE task_id=? AND user_id=? AND is_completed=0",
                        (int(task_id), uid),
                    )
                    completed += cursor.rowcount
                except (TypeError, ValueError):
                    continue

            for item in data.get("new_tasks", []):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                category = normalize_category(
                    str(item.get("category", "")), uid
                )
                conn.execute(
                    "INSERT INTO tasks (user_id, category, task_text) VALUES (?, ?, ?)",
                    (uid, category, text[:500]),
                )
                inserted_tasks += 1

            conn.commit()
        finally:
            conn.close()

    # События сохраняем отдельно. По умолчанию — БЕЗ напоминания:
    # произвольное время берём из голоса/текста только при положительном балансе,
    # иначе предлагаем выбрать время кнопками после фиксации задачи.
    for item in data.get("new_events", []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        local_datetime = item.get("local_datetime")
        if not title or not local_datetime:
            continue

        reminders = []
        if has_paid_balance:
            try:
                remind = int(item.get("remind_minutes"))
            except (TypeError, ValueError):
                remind = None
            if remind and 1 <= remind <= 7 * 1440:
                reminders = [remind]

        event_id = create_event(
            uid,
            title,
            str(local_datetime),
            timezone_name,
            reminders,
        )
        if event_id:
            inserted_events += 1
            if not reminders:
                no_reminder_events.append((event_id, title))

    if use_voice_limit and not premium and free > 0:
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE users SET free_voice_left = MAX(free_voice_left - 1, 0) WHERE user_id=?",
                    (uid,),
                )
                conn.commit()
            finally:
                conn.close()

    return inserted_tasks, inserted_events, completed, no_reminder_events


# ============================================================
# 9. НЕОБЯЗАТЕЛЬНАЯ ОЗВУЧКА
# ============================================================

def synthesize_speech(text, output_path):
    """
    TTS необязателен.
    Нужен TTS_API_URL, поддерживающий OpenAI-compatible /audio/speech.
    Совместимость вашего шлюза предварительно проверьте отдельно.
    """
    if not TTS_API_URL or not TTS_API_KEY:
        return False

    response = requests.post(
        TTS_API_URL,
        headers={"Authorization": f"Bearer {TTS_API_KEY}"},
        json={
            "model": TTS_MODEL,
            "voice": TTS_VOICE,
            "input": text[:3500],
            # opus = OGG/Opus — родной формат голосовых сообщений Telegram.
            "response_format": "opus",
        },
        timeout=(15, 90),
    )

    if response.status_code != 200:
        logger.warning("TTS failed: %s %s", response.status_code, response.text[:500])
        return False

    with open(output_path, "wb") as output:
        output.write(response.content)
    return True


def send_answer(uid, text):
    """Ответ пользователю: голосом, если озвучка доступна, иначе текстом.

    Озвучка активна при положительном балансе либо за счёт пробных
    озвучек (первые FREE_TTS_TRIES при подключении). Стоимость
    ≈ PRICE_TTS_RUB_PER_1K ₽/1000 знаков, учитывается в /report.
    """
    _tz, _offsets, voice_enabled = get_user_preferences(uid)

    paid, bonus, tts_free, _chat, _img, _show = get_balance(uid)
    tts_cost = round(len(text[:3500]) / 1000 * PRICE_TTS_RUB_PER_1K, 3)
    # Бесплатная пробная озвучка имеет приоритет над балансом.
    use_trial = tts_free > 0
    use_paid = not use_trial and (paid + bonus) >= tts_cost

    if voice_enabled and TTS_API_URL and TTS_API_KEY and (use_trial or use_paid):
        path = os.path.join(tempfile.gettempdir(), f"voiceplan_tts_{uid}_{uuid.uuid4().hex}.ogg")
        try:
            if synthesize_speech(text, path):
                with open(path, "rb") as audio:
                    bot.send_voice(uid, audio)
                if use_trial:
                    consume_tts_trial(uid)
                else:
                    spend_from_balance(uid, tts_cost)
                    maybe_receipt(uid, tts_cost, "spend_tts")
                log_ai_request(
                    uid,
                    "tts",
                    cost_rub=round(len(text[:3500]) / 1000 * TTS_PRICE_RUB_PER_1K, 3),
                )
                return
        except Exception:
            logger.exception("TTS generation failed")
        finally:
            if os.path.exists(path):
                os.remove(path)

    # Фолбэк: текст, разбитый по лимиту Telegram (4096 символов).
    for start in range(0, len(text), 4000):
        bot.send_message(uid, text[start:start + 4000])


# ============================================================
# 10. ОБРАБОТЧИКИ ГОЛОСА И ТЕКСТА
# ============================================================

def send_reminder_picker(chat_id, uid, event_id, title):
    """Предложить выбрать время напоминания для события (кнопки + своё время)."""
    short = REMINDER_SHORT.get(get_user_lang(uid), REMINDER_SHORT["ru"])
    markup = types.InlineKeyboardMarkup(row_width=2)
    for key in REMINDER_QUICK:
        markup.add(
            types.InlineKeyboardButton(short[key], callback_data=f"evtrem:{event_id}:{key}")
        )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "remind_custom_btn"), callback_data=f"evtremc:{event_id}")
    )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "remind_skip"), callback_data=f"evtrems:{event_id}")
    )
    bot.send_message(chat_id, tr(uid, "remind_pick", title=title), reply_markup=markup)


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    uid = message.from_user.id
    if not oferta_gate(message):
        return
    log_activity(uid)
    free, premium = get_user_data(uid)

    if free <= 0 and not premium:
        bot.send_message(uid, tr(uid, "premium", free=free))
        return

    status = bot.reply_to(message, tr(uid, "processing"))
    path = os.path.join(tempfile.gettempdir(), f"voiceplan_{uid}_{message.message_id}.ogg")

    try:
        file_info = bot.get_file(message.voice.file_id)
        audio = bot.download_file(file_info.file_path)

        with open(path, "wb") as output:
            output.write(audio)

        transcript = transcribe_voice(path)
        if not transcript:
            bot.edit_message_text(tr(uid, "no_speech"), message.chat.id, status.message_id)
            return

        result = analyze_user_text(
            uid,
            transcript,
            kind="voice",
            audio_seconds=getattr(message.voice, "duration", 0) or 0,
        )
        counts = save_ai_result(uid, result, use_voice_limit=True)

        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass

        summary = tr(uid, "task_added")
        bot.send_message(message.chat.id, summary)
        board, markup = dashboard(uid)
        bot.send_message(message.chat.id, board, reply_markup=markup)

        # Если создавались события — показываем актуальный календарь.
        if counts[1]:
            events_text, events_markup = upcoming_events_text(uid)
            bot.send_message(message.chat.id, events_text, reply_markup=events_markup)

        # События без напоминания — предлагаем выбрать время (кнопками/своё).
        for event_id, title in counts[3]:
            send_reminder_picker(message.chat.id, uid, event_id, title)

    except Exception:
        logger.exception("Voice handling failed for user %s", uid)
        try:
            bot.edit_message_text(tr(uid, "api_error"), message.chat.id, status.message_id)
        except Exception:
            bot.send_message(message.chat.id, tr(uid, "api_error"))
    finally:
        if os.path.exists(path):
            os.remove(path)


def handle_plain_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if not text:
        return

    if not oferta_gate(message):
        return

    log_activity(uid)

    # Ввод своего времени напоминания для конкретного события.
    state = user_states.get(uid)
    if isinstance(state, str) and state.startswith("custom_reminder:"):
        event_id = state.split(":", 1)[1]
        minutes = parse_custom_reminder(text)
        if minutes is None:
            bot.reply_to(message, tr(uid, "remind_custom_bad"))
            return
        try:
            event_id_int = int(event_id)
        except (TypeError, ValueError):
            user_states.pop(uid, None)
            bot.reply_to(message, tr(uid, "api_error"))
            return
        set_event_reminder(uid, event_id_int, minutes)
        user_states.pop(uid, None)
        bot.reply_to(
            message, tr(uid, "remind_done", value=format_reminder_labels(uid, [minutes]))
        )
        return

    # Ввод часового пояса.
    if user_states.get(uid) == "timezone":
        try:
            ZoneInfo(text)
        except ZoneInfoNotFoundError:
            bot.reply_to(message, tr(uid, "unknown_timezone"))
            return

        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE users SET timezone=? WHERE user_id=?",
                    (text, uid),
                )
                conn.commit()
            finally:
                conn.close()

        user_states.pop(uid, None)
        bot.send_message(uid, tr(uid, "settings"), reply_markup=main_keyboard(uid))
        return

    # Создание пользовательской папки.
    if user_states.get(uid) == "category":
        if len(text) > 60:
            bot.reply_to(message, "Название папки должно быть не длиннее 60 символов.")
            return

        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    "INSERT INTO custom_categories (user_id, category_name) VALUES (?, ?)",
                    (uid, text),
                )
                conn.commit()
            finally:
                conn.close()

        user_states.pop(uid, None)
        bot.send_message(uid, tr(uid, "category_saved", name=text), reply_markup=main_keyboard(uid))
        return

    # Режим «💬 Чат с ИИ»: обычный вопрос — ответ текстовой нейросети.
    if user_states.get(uid) == "chat":
        free, premium = get_user_data(uid)
        paid, bonus, tts_free, chat_left, _img, _show = get_balance(uid)

        # Порядок списания: дневная бесплатная норма → баланс → отказ.
        # Легаси-Premium (старая подписка) ходит без списания.
        spend_amount = 0.0
        daily_free = True
        if premium:
            pass
        elif chat_left > 0:
            pass
        elif paid + bonus >= PRICE_CHAT_RUB:
            daily_free = False
            spend_amount = PRICE_CHAT_RUB
        else:
            bot.reply_to(
                message, tr(uid, "tries_limit"), reply_markup=balance_open_markup(uid)
            )
            return

        status = bot.reply_to(message, tr(uid, "processing"))
        try:
            answer = ai_chat_reply(uid, text)
            if daily_free and not premium:
                consume_daily(uid, "chat")
            elif not daily_free:
                spend_from_balance(uid, spend_amount)
                maybe_receipt(uid, spend_amount, "spend_chat")
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    tr(uid, "btn_exit_chat"), callback_data="chat:exit"
                )
            )
            # Озвучка ответов: голосом при включённой настройке, иначе текстом.
            _tzo, _ofz, voice_on = get_user_preferences(uid)
            if voice_on:
                send_answer(uid, answer)
            else:
                # Сообщение Telegram не длиннее 4096 символов — режем на части.
                for start in range(0, len(answer), 4000):
                    chunk = answer[start:start + 4000]
                    bot.send_message(
                        message.chat.id,
                        chunk,
                        reply_markup=markup if start + 4000 >= len(answer) else None,
                    )
        except Exception:
            logger.exception("AI chat failed for user %s", uid)
            bot.send_message(message.chat.id, tr(uid, "api_error"))
        return

    # Режим «🎨 Картинки»: текст — промпт для генерации изображения.
    if user_states.get(uid) == "image_prompt":
        free, premium = get_user_data(uid)
        paid, bonus, tts_free, _chat, image_left, _show = get_balance(uid)

        # Легаси-Premium (старая подписка) ходит без списания.
        spend_amount = 0.0
        daily_free = True
        if premium:
            pass
        elif image_left > 0:
            pass
        elif paid + bonus >= PRICE_IMAGE_RUB:
            daily_free = False
            spend_amount = PRICE_IMAGE_RUB
        else:
            bot.reply_to(
                message, tr(uid, "tries_limit"), reply_markup=balance_open_markup(uid)
            )
            return

        status = bot.reply_to(message, tr(uid, "image_generating"))
        try:
            photo = generate_image(uid, text)
            if premium:
                caption = tr(uid, "image_done_paid")
            elif daily_free:
                consume_daily(uid, "image")
                _p, _b, _t, _c, image_after, _s = get_balance(uid)
                caption = tr(uid, "image_done", left=image_after)
            else:
                spend_from_balance(uid, spend_amount)
                maybe_receipt(uid, spend_amount, "spend_image")
                caption = tr(uid, "image_done_paid")
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
            bot.send_photo(
                message.chat.id,
                photo,
                caption=caption,
            )
        except Exception:
            logger.exception("Image generation failed for user %s", uid)
            bot.send_message(message.chat.id, tr(uid, "api_error"))
        return

    status = bot.reply_to(message, tr(uid, "processing"))
    try:
        result = analyze_user_text(uid, text)
        counts = save_ai_result(uid, result, use_voice_limit=False)
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass

        board, markup = dashboard(uid)
        bot.send_message(message.chat.id, tr(uid, "task_added"))
        bot.send_message(message.chat.id, board, reply_markup=markup)

        if result.get("new_events"):
            event_text, event_markup = upcoming_events_text(uid)
            bot.send_message(message.chat.id, event_text, reply_markup=event_markup)

        # События без напоминания — предлагаем выбрать время (кнопками/своё).
        for event_id, title in counts[3]:
            send_reminder_picker(message.chat.id, uid, event_id, title)

    except Exception:
        logger.exception("Text handling failed for user %s", uid)
        try:
            bot.edit_message_text(tr(uid, "api_error"), message.chat.id, status.message_id)
        except Exception:
            bot.send_message(message.chat.id, tr(uid, "api_error"))


# ============================================================
# 11. СОБЫТИЯ И НАПОМИНАНИЯ
# ============================================================

def reminder_worker():
    """Проверяет базу каждую минуту; напоминания переживают перезапуск процесса."""
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with db_lock:
                conn = get_db()
                try:
                    rows = conn.execute(
                        """SELECT r.reminder_id, r.event_id, r.user_id, e.title,
                                  e.start_utc, e.timezone
                           FROM reminders r
                           JOIN events e ON e.event_id=r.event_id
                           WHERE r.sent=0 AND r.due_utc<=? AND e.cancelled=0
                           ORDER BY r.due_utc LIMIT 100""",
                        (now,),
                    ).fetchall()

                    # Помечаем отправленными до отправки, чтобы избежать дублей
                    # после перезапуска. При сетевом сбое администратор увидит лог.
                    ids = [row["reminder_id"] for row in rows]
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        conn.execute(
                            f"UPDATE reminders SET sent=1 WHERE reminder_id IN ({placeholders})",
                            ids,
                        )
                        conn.commit()
                finally:
                    conn.close()

            for row in rows:
                uid = row["user_id"]
                try:
                    local_dt = datetime.fromisoformat(row["start_utc"]).astimezone(
                        ZoneInfo(row["timezone"])
                    )
                    date_text = local_dt.strftime("%d.%m.%Y %H:%M")
                    bot.send_message(
                        uid,
                        tr(uid, "reminder", title=row["title"], date=date_text),
                    )
                except Exception:
                    logger.exception("Reminder delivery failed for user %s", uid)

        except Exception:
            logger.exception("Reminder worker error")

        time.sleep(30)


threading.Thread(target=reminder_worker, daemon=True).start()


# ============================================================
# 12. ПАПКИ, НАСТРОЙКИ И ТЕКСТОВЫЕ КОМАНДЫ
# ============================================================

@bot.message_handler(commands=["new_category"])
def new_category_command(message):
    user_states[message.from_user.id] = "category"
    bot.send_message(message.chat.id, tr(message.from_user.id, "category_prompt"))


@bot.message_handler(commands=["settings"])
def settings_command(message):
    settings_menu(message)


@bot.message_handler(commands=["premium"])
@bot.message_handler(commands=["balance"])
def premium_command(message):
    """Экран «💰 Баланс»: остаток, дневные нормы, пополнение, настройки."""
    uid = message.from_user.id
    text, markup = balance_screen(uid)
    bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["support"])
def support_command(message):
    bot.send_message(
        message.chat.id,
        tr(message.from_user.id, "support", contact=SUPPORT_CONTACT),
    )


@bot.message_handler(commands=["chat"])
def chat_command(message):
    """Быстрый вход в режим ИИ-чата из команды."""
    uid = message.from_user.id
    if not oferta_gate(message):
        return
    user_states[uid] = "chat"
    bot.send_message(
        message.chat.id, tr(uid, "chat_started"), reply_markup=main_keyboard(uid)
    )


@bot.message_handler(commands=["image"])
def image_command(message):
    """Быстрый вход в режим генерации картинок из команды."""
    uid = message.from_user.id
    if not oferta_gate(message):
        return
    user_states[uid] = "image_prompt"
    bot.send_message(
        message.chat.id, tr(uid, "image_started"), reply_markup=main_keyboard(uid)
    )


@bot.message_handler(
    func=lambda message: bool(message.text)
    and not message.text.startswith("/")
)
def text_router(message):
    uid = message.from_user.id
    text = message.text

    # Публичная оферта должна быть принята до любых действий.
    if not oferta_gate(message):
        return

    menu_texts = {
        value
        for language in LANGS.values()
        for key, value in language.items()
        if key.startswith("btn_")
    }

    if text in menu_texts:
        chat_buttons = {LANGS["ru"]["btn_chat"], LANGS["en"]["btn_chat"]}
        image_buttons = {LANGS["ru"]["btn_image"], LANGS["en"]["btn_image"]}

        # Любая другая кнопка меню завершает режимы чата и картинки.
        if (
            text not in chat_buttons | image_buttons
            and user_states.get(uid) in {"chat", "image_prompt"}
        ):
            user_states.pop(uid, None)

        if text in chat_buttons:
            user_states[uid] = "chat"
            bot.send_message(
                message.chat.id, tr(uid, "chat_started"), reply_markup=main_keyboard(uid)
            )
            return
        if text in image_buttons:
            user_states[uid] = "image_prompt"
            bot.send_message(
                message.chat.id, tr(uid, "image_started"), reply_markup=main_keyboard(uid)
            )
            return
        if text in {LANGS["ru"]["btn_video"], LANGS["en"]["btn_video"]}:
            bot.send_message(
                message.chat.id, tr(uid, "video_stub"), reply_markup=main_keyboard(uid)
            )
            return
        if text in {LANGS["ru"]["btn_oferta"], LANGS["en"]["btn_oferta"]}:
            send_oferta_prompt(message.chat.id, uid)
            return
        if text in {LANGS["ru"]["btn_planner"], LANGS["en"]["btn_planner"]}:
            return show_tasks(message)
        if text in {LANGS["ru"]["btn_tasks"], LANGS["en"]["btn_tasks"]}:
            return show_tasks(message)
        if text in {LANGS["ru"]["btn_events"], LANGS["en"]["btn_events"]}:
            return show_events(message)
        if text in {LANGS["ru"]["btn_calendar"], LANGS["en"]["btn_calendar"]}:
            return calendar_command(message)
        if text in {LANGS["ru"]["btn_folders"], LANGS["en"]["btn_folders"]}:
            return show_folders(message)
        if text in {LANGS["ru"]["btn_new_cat"], LANGS["en"]["btn_new_cat"]}:
            return new_category_command(message)
        if text in {LANGS["ru"]["btn_settings"], LANGS["en"]["btn_settings"]}:
            return settings_menu(message)
        if text in {LANGS["ru"]["btn_language"], LANGS["en"]["btn_language"]}:
            return choose_language(message)
        if text in {LANGS["ru"]["btn_premium"], LANGS["en"]["btn_premium"]}:
            return premium_command(message)
        if text in {LANGS["ru"]["btn_support"], LANGS["en"]["btn_support"]}:
            return support_command(message)

    handle_plain_text(message)


# ============================================================
# 13. НАПОМИНАНИЯ И НАСТРОЙКИ — CALLBACKS
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("reminder_toggle:"))
def reminder_toggle_router(call):
    toggle_reminder_preference(call)


# ============================================================
# 14. АДМИН-КОМАНДЫ
# ============================================================

@bot.message_handler(commands=["add_free"])
def admin_add_free(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, tr(message.from_user.id, "not_admin"))
        return

    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.reply_to(message, tr(message.from_user.id, "grant_usage"))
        return

    target = int(parts[1])
    amount = int(parts[2])
    if amount <= 0 or amount > 10000:
        bot.reply_to(message, "Укажите количество от 1 до 10000.")
        return

    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute(
                "UPDATE users SET free_voice_left=free_voice_left+? WHERE user_id=?",
                (amount, target),
            )
            conn.commit()
            changed = cursor.rowcount
        finally:
            conn.close()

    if not changed:
        bot.reply_to(message, "Пользователь не найден.")
        return

    bot.reply_to(message, tr(message.from_user.id, "grant_ok", count=amount))
    try:
        bot.send_message(target, tr(target, "grant_user", count=amount))
    except Exception:
        logger.exception("Не удалось отправить уведомление о начислении")


@bot.message_handler(commands=["free"])
def free_self_command(message):
    """Попытки: /free — остаток; /free N — начислить себе (только админ)."""
    uid = message.from_user.id
    parts = message.text.split()

    if len(parts) == 1:
        # /free — показываем экран баланса (остаток, дневные нормы, пополнение).
        text, markup = balance_screen(uid)
        bot.reply_to(message, text, reply_markup=markup)
        return

    if uid != ADMIN_ID:
        bot.reply_to(message, tr(uid, "not_admin"))
        return

    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, tr(uid, "free_usage"))
        return

    amount = int(parts[1])
    if amount <= 0 or amount > 10000:
        bot.reply_to(message, "Укажите количество от 1 до 10000.")
        return

    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            conn.execute(
                "UPDATE users SET free_voice_left=free_voice_left+? WHERE user_id=?",
                (amount, uid),
            )
            conn.commit()
        finally:
            conn.close()

    free, _premium = get_user_data(uid)
    bot.reply_to(message, tr(uid, "free_added", count=amount, free=free))


def _parse_report_dt(text_value, default_hm):
    """'01.10.2026 10:00' | '01.10.2026' | '01.10 10:00' | '01.10' -> datetime (локальное время)."""
    for fmt, hm in (
        ("%d.%m.%Y %H:%M", None),
        ("%d.%m.%Y", default_hm),
        ("%d.%m %H:%M", None),
        ("%d.%m", default_hm),
    ):
        try:
            dt = datetime.strptime(text_value, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=datetime.now().year)
        if hm:
            hour, minute = hm.split(":")
            dt = dt.replace(hour=int(hour), minute=int(minute))
        return dt
    return None


@bot.message_handler(commands=["report"])
def admin_report(message):
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()[1:]
    if len(args) == 2:
        start_local = _parse_report_dt(args[0], "00:00")
        end_local = _parse_report_dt(args[1], "23:59")
    elif len(args) == 4:
        start_local = _parse_report_dt(f"{args[0]} {args[1]}", None)
        end_local = _parse_report_dt(f"{args[2]} {args[3]}", None)
    else:
        bot.reply_to(message, tr(message.from_user.id, "report_usage"))
        return

    if not start_local or not end_local or end_local < start_local:
        bot.reply_to(message, tr(message.from_user.id, "report_usage"))
        return

    tz_name, _off, _voice = get_user_preferences(ADMIN_ID)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc

    fmt_utc = "%Y-%m-%d %H:%M:%S"
    start_utc = start_local.replace(tzinfo=tz).astimezone(timezone.utc)
    end_utc = (end_local + timedelta(minutes=1)).replace(tzinfo=tz).astimezone(timezone.utc)
    s, e = start_utc.strftime(fmt_utc), end_utc.strftime(fmt_utc)

    with db_lock:
        conn = get_db()
        try:
            new_total = conn.execute(
                "SELECT COUNT(*) n FROM users WHERE created_at>=? AND created_at<?",
                (s, e),
            ).fetchone()["n"]
            new_free = conn.execute(
                "SELECT COUNT(*) n FROM users WHERE created_at>=? AND created_at<? AND is_premium=0",
                (s, e),
            ).fetchone()["n"]
            paid_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) n FROM payments WHERE created_at>=? AND created_at<?",
                (s, e),
            ).fetchone()["n"]
            active_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) n FROM activity WHERE created_at>=? AND created_at<?",
                (s, e),
            ).fetchone()["n"]
            req = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost_rub),0) c "
                "FROM ai_requests WHERE created_at>=? AND created_at<?",
                (s, e),
            ).fetchone()
            by_kind = conn.execute(
                "SELECT kind, COUNT(*) n FROM ai_requests "
                "WHERE created_at>=? AND created_at<? GROUP BY kind",
                (s, e),
            ).fetchall()
            pay = conn.execute(
                "SELECT COALESCE(SUM(amount_stars),0) s, COALESCE(SUM(amount_rub),0) r "
                "FROM payments WHERE created_at>=? AND created_at<?",
                (s, e),
            ).fetchone()
        finally:
            conn.close()

    kind_map = {row["kind"]: row["n"] for row in by_kind}
    period_text = (
        f"{start_local.strftime('%d.%m.%Y %H:%M')} — "
        f"{end_local.strftime('%d.%m.%Y %H:%M')} ({tz_name})"
    )
    bot.reply_to(
        message,
        f"📊 ОТЧЁТ за период\n{period_text}\n\n"
        f"👥 Новых пользователей: {new_total}\n"
        f"   • бесплатных: {new_free}\n"
        f"   • платных (с оплатой в периоде): {paid_users}\n"
        f"🟢 Активны в периоде (писали/голосовые): {active_users}\n"
        f"📨 Запросов к ИИ: {req['n']} "
        f"(голос: {kind_map.get('voice', 0)}, текст: {kind_map.get('text', 0)})\n"
        f"💰 Оплат: {pay['s']} XTR / {pay['r']:.2f} ₽\n"
        f"🤖 Расход на ИИ-запросы: {req['c']:.2f} ₽",
    )


@bot.message_handler(commands=["stats_all"])
def admin_stats_all(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db_lock:
        conn = get_db()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            premium = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_premium=1"
            ).fetchone()["n"]
            revenue = conn.execute(
                "SELECT COALESCE(SUM(amount_stars),0) AS n FROM payments"
            ).fetchone()["n"]
            langs = conn.execute(
                "SELECT language, COUNT(*) AS n FROM users GROUP BY language"
            ).fetchall()
        finally:
            conn.close()

    lang_text = "\n".join(f"{row['language']}: {row['n']}" for row in langs) or "—"
    bot.reply_to(
        message,
        f"📊 Всего пользователей: {total}\n"
        f"⭐ Premium-пользователей: {premium}\n"
        f"Бесплатных: {total - premium}\n"
        f"⭐ Получено Stars: {revenue} XTR\n"
        f"🌐 Языки:\n{lang_text}",
    )


@bot.message_handler(commands=["stats_period"])
def admin_stats_period(message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Формат: /stats_period 7")
        return

    days = int(parts[1])
    if days < 1 or days > 3650:
        bot.reply_to(message, "Укажите период от 1 до 3650 дней.")
        return

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    with db_lock:
        conn = get_db()
        try:
            users = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE created_at>=?",
                (since,),
            ).fetchone()["n"]
            stars = conn.execute(
                "SELECT COALESCE(SUM(amount_stars),0) AS n FROM payments WHERE created_at>=?",
                (since,),
            ).fetchone()["n"]
        finally:
            conn.close()

    bot.reply_to(
        message,
        f"📈 За последние {days} дней:\n"
        f"Новых пользователей: {users}\n"
        f"Поступило Stars: {stars} XTR",
    )


# ============================================================
# 15. ОПЛАТА TELEGRAM STARS
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data == "buy_stars")
def buy_stars(call):
    """Старая кнопка «Stars» из прежнего меню → экран выбора пакета."""
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    text, markup = topup_screen(uid)
    _edit_or_send(call, text, markup)


@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def successful_payment(message):
    uid = message.from_user.id
    payment = message.successful_payment
    payload = payment.invoice_payload or ""

    # Новый формат: bal_topup:199 → зачисление на баланс + бонус пакета.
    if payload.startswith("bal_topup:"):
        try:
            rub = int(payload.split(":", 1)[1])
        except ValueError:
            rub = int(payment.total_amount)
        bonus_pct = next((pct for amount, pct in TOPUP_PACKAGES if amount == rub), 0)
        bonus = round(rub * bonus_pct / 100.0, 2)
        with db_lock:
            conn = get_db()
            try:
                conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
                conn.execute(
                    """UPDATE users SET balance_paid = balance_paid + ?,
                       balance_bonus = balance_bonus + ? WHERE user_id = ?""",
                    (float(rub), bonus, uid),
                )
                conn.execute(
                    """INSERT INTO payments(user_id, amount_stars, amount_rub, payment_system)
                       VALUES (?, ?, ?, 'Telegram Stars')""",
                    (uid, payment.total_amount, float(rub)),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT balance_paid, balance_bonus FROM users WHERE user_id = ?",
                    (uid,),
                ).fetchone()
            finally:
                conn.close()
        total = float(row["balance_paid"] or 0) + float(row["balance_bonus"] or 0)
        bot.send_message(
            uid,
            tr(uid, "topup_done", amount=f"{rub:g}", bonus=f"{bonus:g}", total=f"{total:.2f}"),
        )
        if ADMIN_ID:
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"💰 Оплата Stars: user_id={uid}, пакет={rub} ₽ "
                    f"(бонус {bonus:g} ₽), звёзды={payment.total_amount}",
                )
            except Exception:
                logger.exception("Не удалось уведомить администратора об оплате")
        return

    # Легаси-формат «premium_month»: сохраняем прежнее поведение подписки.
    with db_lock:
        conn = get_db()
        try:
            conn.execute("UPDATE users SET is_premium=1 WHERE user_id=?", (uid,))
            conn.execute(
                """INSERT INTO payments(user_id, amount_stars, amount_rub, payment_system)
                   VALUES (?, ?, 0, 'Telegram Stars')""",
                (uid, payment.total_amount),
            )
            conn.commit()
        finally:
            conn.close()

    bot.send_message(uid, "🎉 Оплата получена. Premium активирован.")
    if ADMIN_ID:
        try:
            bot.send_message(
                ADMIN_ID,
                f"💰 Оплата Stars: user_id={uid}, сумма={payment.total_amount} XTR",
            )
        except Exception:
            logger.exception("Не удалось уведомить администратора об оплате")


# ============================================================
# 15-bis. ИИ-ЧАТ, КАРТИНКИ И CALLBACKS НОВОГО ИНТЕРФЕЙСА
# ============================================================

def ai_chat_reply(uid, text):
    """Ответ текстовой нейросети (модель CHAT_MODEL через OpenAI-совместимый API)."""
    if get_user_lang(uid) == "en":
        system_prompt = (
            "You are a friendly assistant inside the VoicePlan Telegram bot. "
            "Answer in the user's language, briefly and to the point "
            "(about 700 characters), without markdown headers."
        )
    else:
        system_prompt = (
            "Ты — дружелюбный ассистент внутри Telegram-бота VoicePlan. "
            "Отвечай на языке пользователя, кратко и по существу "
            "(до 700 символов), без markdown-заголовков."
        )

    response = requests.post(
        f"{AI_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "max_tokens": 1200,
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    log_ai_request(
        uid,
        "chat",
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
    )
    return data["choices"][0]["message"]["content"].strip()


def generate_image(uid, prompt):
    """Генерация картинки (модель IMAGE_MODEL) → bytes."""
    response = requests.post(
        f"{AI_BASE_URL}/images/generations",
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
        },
        timeout=300,
    )
    response.raise_for_status()
    item = response.json()["data"][0]

    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        download = requests.get(item["url"], timeout=180)
        download.raise_for_status()
        raw = download.content
    else:
        raise RuntimeError("В ответе images API нет ни b64_json, ни url")

    log_ai_request(uid, "image", cost_rub=IMAGE_PRICE_RUB)
    return raw


@bot.callback_query_handler(func=lambda call: call.data == "oferta:accept")
def oferta_accept(call):
    uid = call.from_user.id
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            conn.execute(
                "UPDATE users SET oferta_accepted=1 WHERE user_id=?", (uid,)
            )
            conn.commit()
        finally:
            conn.close()

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id, text="✅")

    _paid, _bonus, _tts, chats, images, _show = get_balance(uid)
    bot.send_message(call.message.chat.id, tr(uid, "oferta_accepted_msg"))
    bot.send_message(
        call.message.chat.id,
        tr(
            uid,
            "welcome",
            name=call.from_user.first_name or "друг",
            chats=chats,
            images=images,
            total=f"{_paid + _bonus:.2f}",
        ),
        reply_markup=main_keyboard(uid),
    )


@bot.callback_query_handler(func=lambda call: call.data == "oferta:show")
def oferta_show(call):
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        tr(call.from_user.id, "oferta_text", contact=SUPPORT_CONTACT),
    )


@bot.callback_query_handler(func=lambda call: call.data == "chat:exit")
def chat_exit(call):
    uid = call.from_user.id
    user_states.pop(uid, None)
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id, tr(uid, "chat_exited"), reply_markup=main_keyboard(uid)
    )


@bot.callback_query_handler(func=lambda call: call.data == "pay_yookassa")
def pay_yookassa(call):
    """Заглушка: подключение ЮKassa будет позже."""
    bot.answer_callback_query(
        call.id,
        text=tr(call.from_user.id, "yookassa_stub"),
        show_alert=True,
    )


# ------------------------------------------------------------
# CALLBACKS БАЛАНСА: показ, пополнение, справки, настройки.
# ------------------------------------------------------------

def _edit_or_send(call, text, markup):
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "bal:show")
def bal_show(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    text, markup = balance_screen(uid)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data == "bal:topup")
def bal_topup(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    text, markup = topup_screen(uid)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("bal:buy:"))
def bal_buy(call):
    """Инвойс Telegram Stars на пакет 199/490/990 ₽ (бонус начисляется после оплаты)."""
    uid = call.from_user.id
    try:
        rub = int(call.data.split(":")[-1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid package")
        return
    package = next((p for p in TOPUP_PACKAGES if p[0] == rub), None)
    if package is None:
        bot.answer_callback_query(call.id, "Invalid package")
        return
    bot.answer_callback_query(call.id)
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="VoicePlan — баланс",
        description=f"Пополнение баланса на {rub} ₽ (пакет через Telegram Stars).",
        invoice_payload=f"bal_topup:{rub}",
        provider_token="",
        currency="XTR",
        prices=[types.LabeledPrice(label=f"Баланс {rub} ₽", amount=rub)],
    )


@bot.callback_query_handler(func=lambda call: call.data == "bal:free")
def bal_free(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        tr(
            uid,
            "free_gift_text",
            chats=FREE_CHATS_PER_DAY,
            images=FREE_IMAGES_PER_DAY,
            tts=FREE_TTS_TRIES,
        ),
    )


@bot.callback_query_handler(func=lambda call: call.data == "bal:how")
def bal_how(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        tr(uid, "how_balance_text"),
    )


@bot.callback_query_handler(func=lambda call: call.data == "bal:save")
def bal_save(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        tr(
            uid,
            "save_balance_text",
            chats=FREE_CHATS_PER_DAY,
            images=FREE_IMAGES_PER_DAY,
        ),
    )


@bot.callback_query_handler(func=lambda call: call.data == "bal:spends")
def bal_spends(call):
    uid = call.from_user.id
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT show_spends FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            value = 0 if (row and row["show_spends"]) else 1
            conn.execute(
                "UPDATE users SET show_spends = ? WHERE user_id = ?", (value, uid)
            )
            conn.commit()
        finally:
            conn.close()
    bot.answer_callback_query(call.id, tr(uid, "spends_on" if value else "spends_off"))
    text, markup = balance_screen(uid)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data == "bal:close")
def bal_close(call):
    bot.answer_callback_query(call.id, "✅")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


# ============================================================
# 16. ЗАПУСК
# ============================================================

if __name__ == "__main__":
    try:
        # Не запускайте другой процесс polling с этим же токеном.
        bot.remove_webhook()
    except Exception:
        logger.exception("Не удалось удалить webhook")

    logger.info("VoicePlan запущен")

    # 409 Conflict возникает, если параллельно работает другой инстанс с тем же
    # токеном (деплой на Render поверх старого процесса, второй сервис, локальный
    # запуск). Вместо падения ждём, пока «соперник» завершится, и пробуем снова.
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
            )
            break  # infinity_polling в норме не возвращается — но на всякий случай
        except ApiTelegramException as e:
            if "409" in str(e) or "Conflict" in str(e):
                logger.warning(
                    "409 Conflict: другой инстанс бота ещё опрашивает Telegram, "
                    "жду 15 секунд и повторяю…"
                )
                time.sleep(15)
                continue
            raise
        except Exception:
            logger.exception("Polling упал, повтор через 10 секунд…")
            time.sleep(10)

