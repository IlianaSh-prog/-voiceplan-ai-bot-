import os
import sys
import json
import time
import base64
import tempfile
import uuid
import hashlib
import calendar as calmod
import sqlite3
import logging
import socket
import threading
import html
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException


def _esc(value):
    """Экранирование пользовательского текста для HTML parse_mode."""
    return html.escape(str(value), quote=False)


# ============================================================
# 1. ЛОГИРОВАНИЕ И HEALTH CHECK ДЛЯ RENDER
# ============================================================

# Диагностика: пишем лог и в консоль, и в файл — чтобы сбои (например, обрыв
# отправки или ошибки ИИ) было видно даже когда бот запущен фоном и чёрное окно
# закрыто. Файл рядом с ботом; путь можно переопределить через LOG_PATH.
LOG_PATH = os.environ.get("LOG_PATH", "voiceplan.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
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


def create_health_server():
    """Создаёт health-сервер для Render (Render ждёт HTTP 200 на порту PORT)."""
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server listening on port %s", port)
    return server


def _port_in_use(port):
    """True, если на порту уже кто-то СЛУШАЕТ (например, другой экземпляр бота).

    Проверяем именно подключением (connect), а не привязкой (bind): на Windows
    два сокета с SO_REUSEADDR могут занять один порт, поэтому bind — ненадёжный
    «замок». А вот к слушающему сокету подключение проходит всегда.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


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
# Качество картинки: "low" (~1.5₽), "medium" (~3.5₽), "high" (~15₽).
IMAGE_QUALITY = os.environ.get("IMAGE_QUALITY", "low")

# Балансная модель: цены для пользователя и дневные бесплатные нормы.
# Чат — пофактовая тарификация (по образцу UnlimAI): цена запроса зависит от его
# объёма и объёма ответа: (токены входа × LLM_PRICE_IN_RUB + токены ответа ×
# LLM_PRICE_OUT_RUB) / 1M × AI_MARKUP, в пределах [CHAT_MIN_RUB; CHAT_MAX_RUB].
AI_MARKUP = float(os.environ.get("AI_MARKUP", "5"))
CHAT_MIN_RUB = float(os.environ.get("CHAT_MIN_RUB", "0.10"))
CHAT_MAX_RUB = float(os.environ.get("CHAT_MAX_RUB", "2"))
# Типичная цена запроса — только для калькулятора пакетов «на что хватит».
CHAT_TYPICAL_RUB = float(os.environ.get("CHAT_TYPICAL_RUB", "0.5"))
# Озвучка: за 1000 знаков текста (себестоимость TTS_PRICE_RUB_PER_1K ≈ 1.5 ₽).
PRICE_TTS_RUB_PER_1K = float(os.environ.get("PRICE_TTS_RUB_PER_1K", "3"))
# Картинки — цена для пользователя по качеству (наценка ×2 к себестоимости —
# верх рыночного диапазона 50–100%; прежняя фиксированная 9 ₽ была наценкой 500%).
IMAGE_PRICES_RUB = {
    "low": float(os.environ.get("PRICE_IMAGE_LOW_RUB", "3")),
    "medium": float(os.environ.get("PRICE_IMAGE_MEDIUM_RUB", "7")),
    "high": float(os.environ.get("PRICE_IMAGE_HIGH_RUB", "30")),
}
# Себестоимость картинки по качеству — для админ-отчёта (/report).
IMAGE_COSTS_RUB = {"low": 1.5, "medium": 3.5, "high": 15.0}
FREE_CHATS_PER_DAY = int(os.environ.get("FREE_CHATS_PER_DAY", "3"))
# Разовые подарки при регистрации (не сбрасываются ежедневно): одна картинка
# и 5 голосовых задач ≈ 6.5 ₽ себестоимости на нового пользователя —
# потолок накладных расходов на бесплатку при любом наплыве регистраций.
FREE_IMAGES_ONCE = int(os.environ.get("FREE_IMAGES_ONCE", "1"))
# Пробные озвучки выдаются один раз при подключении (не ежедневно).
FREE_TTS_TRIES = int(os.environ.get("FREE_TTS_TRIES", "3"))
# Голосовые задачи: разово при регистрации (Whisper — основная себестоимость).
# Сверх квоты — списание VOICE_TASK_PRICE_RUB с баланса, иначе отказ.
# 3 ₽ при себестоимости ≈1.5 ₽/мин — наценка 100%, как у рыночных ботов.
FREE_VOICE_TASKS_ONCE = int(os.environ.get("FREE_VOICE_TASKS_ONCE", "5"))
VOICE_TASK_PRICE_RUB = float(os.environ.get("VOICE_TASK_PRICE_RUB", "3"))
# Лимиты бесплатного чата: длина запроса и объём ответа. Платящим за токены
# лимитов нет (их защищает только потолок CHAT_MAX_RUB) — ограничения нужны
# именно бесплатке, чтобы 3 чата/день стоили ≈0.17 ₽, а не копейки «с запасом».
FREE_MSG_MAX_CHARS = int(os.environ.get("FREE_MSG_MAX_CHARS", "2000"))
FREE_ANSWER_MAX_TOKENS = int(os.environ.get("FREE_ANSWER_MAX_TOKENS", "800"))
PAID_ANSWER_MAX_TOKENS = int(os.environ.get("PAID_ANSWER_MAX_TOKENS", "4000"))
# Лаконичный режим: при балансе ниже порога отвечаем коротко
# (SHORT_ANSWER_MAX_TOKENS ≈ до 200 знаков), чтобы фактическая цена запроса
# гарантированно не превысила остаток — без ухода в минус и без клампа.
CHAT_SHORT_MODE_RUB = float(os.environ.get("CHAT_SHORT_MODE_RUB", "0.50"))
SHORT_ANSWER_MAX_TOKENS = int(os.environ.get("SHORT_ANSWER_MAX_TOKENS", "80"))
# Защита бесплатки от наплыва регистраций и «пауза», пока админ не в сети:
# • /guard on|off — мгновенный стоп/пуск бесплатных бонусов без деплоя;
# • USER_MILESTONES — вехи роста: при пересечении админу уходит просьба
#   пересмотреть условия бесплатных возможностей;
# • MAX_NEW_USERS_PER_DAY — потолок новых регистраций с подарками в сутки,
#   сверх него аккаунты создаются без бесплатных норм (free_off=1).
#   Расходы на подарки при этом ограничены: ≈6.5 ₽ × MAX_NEW_USERS_PER_DAY/сутки.
USER_MILESTONES = tuple(
    int(x)
    for x in os.environ.get(
        "USER_MILESTONES", "100,250,500,1000,2500,5000,10000,25000,50000,100000"
    ).split(",")
    if x.strip()
)
MAX_NEW_USERS_PER_DAY = int(os.environ.get("MAX_NEW_USERS_PER_DAY", "300"))
# Флуд-защита регистраций (скользящее окно на ВСЕ новые аккаунты):
# • превышение SIGNUP_CAPTCHA_LIMIT за окно → новые пользователи подтверждают
#   «Я не робот» (одноразовый токен с TTL), подарки выдаются после нажатия;
# • превышение SIGNUP_BURST_LIMIT за окно → guard включается АВТОМАТИЧЕСКИ
#   (+ срочное оповещение админу с повтором); после автостопа капча
#   действует ещё SIGNUP_CAPTCHA_TTL секунд кулдауна.
SIGNUP_BURST_WINDOW = int(os.environ.get("SIGNUP_BURST_WINDOW", "10"))
SIGNUP_BURST_LIMIT = int(os.environ.get("SIGNUP_BURST_LIMIT", "20"))
SIGNUP_CAPTCHA_LIMIT = int(os.environ.get("SIGNUP_CAPTCHA_LIMIT", "5"))
SIGNUP_CAPTCHA_TTL = int(os.environ.get("SIGNUP_CAPTCHA_TTL", "900"))
SIGNUP_CAPTCHA_TIMEOUT = int(os.environ.get("SIGNUP_CAPTCHA_TIMEOUT", "300"))
# Свежий ID (C-lite): аккаунт, моложе всех ранее виденных ботом. ID Telegram
# выдаются монотонно, поэтому «uid > границы seen» ≈ «создан позже всех» —
# дешёвая эвристика одноразового свежесозданного аккаунта: капча ставится даже
# одной регистрацией, без порогов всплеска (добивает drip-атаку по 1 аккаунту).
# Граница — bot_meta.uid_hi, инициализируется по MAX(user_id) из истории.
# Отключение: SIGNUP_FRESH_CAPTCHA=0.
SIGNUP_FRESH_CAPTCHA = os.environ.get("SIGNUP_FRESH_CAPTCHA", "1") != "0"
# Пакеты пополнения через Telegram Stars: (сумма ₽, бонус %).
# «Серию дней» и другие ежедневные подарки сознательно не делаем —
# постоянные списания «в подарок» невыгодны при бесплатных пользователях.
TOPUP_PACKAGES = ((199, 0), (490, 5), (990, 10))
# Планировщик Pro: отдельный продукт (календарь + напоминания) без ИИ.
# Себестоимость ≈ 0, поэтому это почти чистая маржа. Доступ на N дней.
PLANNER_PRICE_RUB = int(os.environ.get("PLANNER_PRICE_RUB", "99"))
PLANNER_DAYS = int(os.environ.get("PLANNER_DAYS", "30"))

# Антифлуд: скользящее окно на пользователя для текстовых и голосовых сообщений.
# Не более FLOOD_MAX_EVENTS сообщений за FLOOD_WINDOW_SEC секунд;
# админ не ограничивается. Настройки можно менять через env без правки кода.
FLOOD_WINDOW_SEC = float(os.environ.get("FLOOD_WINDOW_SEC", "10"))
FLOOD_MAX_EVENTS = int(os.environ.get("FLOOD_MAX_EVENTS", "6"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная TELEGRAM_BOT_TOKEN")

if not AI_API_KEY:
    logger.warning("AI_API_KEY не задан. Голосовая расшифровка и ИИ-разбор не будут работать.")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
db_lock = threading.RLock()
user_states = {}
# Последний ответ ИИ в чате по каждому пользователю — чтобы кнопка «Озвучить»
# могла озвучить именно его (callback не может нести длинный текст).
last_answer = {}

SYSTEM_CATEGORIES = {
    "ru": ["🔴 Срочно", "📋 Повседневное", "⭐ Важно"],
    "en": ["🔴 Urgent", "📋 Daily", "⭐ Important"],
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
        "task_reminder": "⏰ Напоминание о задаче: {title}\n📁 Папка: {category}",
        "reminder_options": "Напоминания: {value}",
        "event_card": (
            "📅 Событие: {title}\n"
            "🕒 {date}"
        ),
        "premium": "🎙 Бесплатных голосовых разборов осталось: {free}.",
        "not_admin": "Команда доступна только администратору.",
        "flood_warn": (
            "⏳ Вы отправляете сообщения слишком часто. "
            "Пожалуйста, подождите пару секунд и попробуйте снова."
        ),
        "grant_usage": "Формат: `/add_free USER_ID КОЛИЧЕСТВО`",
        "grant_ok": "✅ Добавлено попыток: {count}.",
        "grant_user": "🎁 Вам начислено голосовых разборов: +{count}.",
        "support": "💬 По вопросам поддержки напишите: {contact}",
        "unknown_timezone": "Не удалось распознать часовой пояс. Проверьте написание, например Europe/Moscow.",
        "confirm_event": "Событие сохранено.",
        "cal_title": "🗓 Календарь — {month}",
        "cal_legend": "• — дни с планами. Нажмите на дату, чтобы открыть её и добавить план.",
        "cal_no_dates": "В этом месяце событий нет.",
        "cal_day_title": "📅 {date}:",
        "cal_today": "📍 Сегодня",
        "cal_back": "⬅️ К календарю",
        "cal_delete_btn": "🗑 Удалить",
        "cal_delete_confirm_title": "🗑 Удалить план?",
        "cal_delete_confirm_body": "«{title}» будет удалено безвозвратно. Точно удалить?",
        "cal_legend_new": (
            "🟢 — дни с планами.\n"
            "📍 — выбранная дата.\n"
            "Нажмите «Выбрать дату», затем наговорите или напишите событие или задачу."
        ),
        "cal_legend_trial": "🎁 Бесплатный календарь: осталось {days} дн.",
        "cal_pick_date": "📍 Выбрать дату",
        "cal_pick_date_prompt": (
            "📍 На какую дату планируем?\n\n"
            "Напишите дату в формате ДД.ММ.ГГГГ — например, 25.10.2026.\n"
            "Или просто: «сегодня», «завтра», «послезавтра»."
        ),
        "cal_pick_date_bad": (
            "Не удалось распознать дату. Примеры: 25.10.2026, завтра, сегодня, послезавтра."
        ),
        "cal_date_set": (
            "✅ Дата выбрана: {date}\n\n"
            "Теперь наговорите или напишите, что запланировано."
        ),
        "cal_add_hint": (
            "✍️ Наговорите или напишите событие или задачу на {date}.\n\n"
            "Можно сразу попросить напомнить — например, «встреча в 15:00, "
            "напомни за час»."
        ),
        "cal_day_empty": (
            "📭 В этот день пока ничего не запланировано.\n\n"
            "Наговорите или напишите событие или задачу — и оно появится здесь."
        ),
        "cal_tap_day_hint": (
            "👇 Нажмите на дату, чтобы открыть её и наговорить план."
        ),
        "cal_day_reminders_title": "⏰ Напомнить:",
        "cal_day_add_reminder": "⏰ Поставить напоминание",
        "cal_day_pick_format": (
            "⏰ За сколько напомнить?\n\n"
            "Нажмите кнопку с готовым вариантом или напишите свой текст, "
            "например: «за 1 час», «за 30 минут», «за 2 дня»."
        ),
        "cal_day_locked": (
            "🔒 Напоминания доступны при положительном балансе. "
            "Пополните баланс, чтобы получать напоминания о планах."
        ),
        "cal_day_selected_for_add": (
            "📅 Дата выбрана: {date}.\n\n"
            "Теперь наговорите или напишите событие или задачу на эту дату. "
            "Можно сразу попросить напомнить — например, «встреча в 15:00, "
            "напомни за час»."
        ),
        "cal_access_needed": (
            "🔒 Календарь с напоминаниями доступен при положительном балансе.\n\n"
            "Первые 7 дней календарь работает бесплатно. Ваш пробный период "
            "завершился — пополните баланс, чтобы продолжить планировать "
            "и получать напоминания."
        ),
        "cal_trial_welcome": (
            "🎁 Добро пожаловать в VoicePlan!\n\n"
            "Мы дарим вам неделю бесплатного доступа к календарю и напоминаниям — "
            "целых 7 дней, начиная с сегодняшнего дня.\n\n"
            "За это время вы сможете:\n"
            "• создавать события и задачи на любую дату;\n"
            "• получать напоминания заранее — за 15 минут, час, день или в свой срок;\n"
            "• планировать голосом или текстом прямо в чате.\n\n"
            "По истечении 7 дней календарь и напоминания остаются доступны при "
            "положительном балансе. Приятного планирования!"
        ),
        "cal_reminder_free_hint": (
            "⏰ Напоминания к событиям подключаются при положительном балансе."
        ),
        "cal_reminder_added": "✅ Напоминание добавлено: {value}.",
        "cal_remind_change_btn": "🔄 Изменить напоминание",
        "cal_remind_off_btn": "🚫 Убрать напоминание",
        "cal_reminder_current": "Текущее напоминание: {value}. Выберите новое или уберите:",
        "evt_back": "⬅️ К списку событий",
        "cal_reminder_below": (
            "⏰ Кнопка под планом — поставить напоминание "
            "(например, за 15 минут или за 1 час)."
        ),
        "free_usage": "Формат: /free 10 — начислить попытки себе.\nБез аргументов — показать остаток.",
        "free_added": "🎁 Начислено попыток: +{count}. Всего теперь: {free}.",
        "setbal_usage": (
            "Формат: /setbalance <сумма> [бонус]\n"
            "Примеры: /setbalance 500 — платный баланс 500 ₽, бонус 0\n"
            "/setbalance 0 — обнулить весь баланс\n"
            "/setbalance 100 50 — платный 100 ₽, бонусный 50 ₽\n"
            "Без аргументов — показать текущий баланс."
        ),
        "setbal_done": "✅ Баланс обновлён: платный {paid} ₽, бонусный {bonus} ₽.",
        "btn_chat": "💬 Чат с ИИ",
        "btn_image": "🎨 Картинки",
        "btn_video": "🎬 Видео",
        "btn_oferta": "📖 Оферта",
        "btn_exit_chat": "↩️ Выйти из чата",
        "btn_speak": "🔊 Озвучить",
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
            "и пробные озвучки; лимиты обновляются 1 раз в день. Первые 7 дней "
            "календарь и напоминания бесплатны для всех.\n"
            "3. Платное: (а) ИИ-баланс — чат, картинки и озвучка оплачиваются "
            "по факту использования с баланса через Telegram Stars: цена чата "
            "зависит от объёма запроса и ответа, цена картинки — от качества, "
            "цена озвучки — от длины текста (озвучка одного и того же текста "
            "списывается один раз); (б) Планировщик Pro — "
            "календарь и напоминания без ИИ, 99 ₽ за 30 дней. Любой положительный "
            "ИИ-баланс также открывает календарь. Автопродления нет — пополнение "
            "и покупка Планировщика по желанию.\n"
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
        "free_paused": (
            "🛡 Бесплатные возможности временно приостановлены "
            "(режим защиты).\nПродолжить работу поможет пополнение баланса."
        ),
        "free_off_notice": (
            "⛔ Бесплатные возможности для вашего аккаунта не активны.\n"
            "Работа доступна с баланса. Вопросы — администратору."
        ),
        "captcha_required": (
            "🛡 Сейчас высокая активность регистраций — подтвердите, "
            "что вы человек, чтобы получить стартовые подарки.\n"
            "Кнопка действует {sec} сек. Если истекла — отправьте /start ещё раз."
        ),
        "captcha_btn": "✅ Я не робот",
        "captcha_toast_ok": "Подтверждено!",
        "captcha_expired": "Срок истёк — отправьте /start ещё раз.",
        "captcha_done": "✅ Человек подтверждён.",
        "captcha_granted": (
            "✅ Спасибо! Стартовые подарки зачислены: {chats} чатов в день, "
            "{img} картинка, {voice} голосовых задач, {tts} пробных озвучек."
        ),
        "captcha_needed": (
            "🤖 Нужно подтвердить, что вы не робот: нажмите кнопку "
            "«✅ Я не робот» выше. Если её нет — отправьте /start."
        ),
        "guard_status_on": (
            "🛡 Режим защиты: ВКЛ.\n"
            "Бесплатные подарки и дневные квоты приостановлены, новые "
            "пользователи создаются без бесплатных норм — работают только "
            "балансы и Premium.\n"
            "Пользователей всего: {total}, новых сегодня: {today} "
            "(лимит регистраций с подарками: {cap}).\n"
            "Флуд-защита: автостоп при {burst} регистрациях за {window} сек, "
            "капча при {captcha} за окно.\n"
            "Выключить: /guard off"
        ),
        "guard_status_off": (
            "🛡 Режим защиты: ВЫКЛ.\n"
            "Бесплатные подарки и квоты работают штатно.\n"
            "Пользователей всего: {total}, новых сегодня: {today} "
            "(лимит регистраций с подарками: {cap}).\n"
            "Флуд-защита: автостоп при {burst} регистрациях за {window} сек, "
            "капча при {captcha} за окно.\n"
            "Включить: /guard on"
        ),
        "bal_open": "💰 Посмотреть баланс",
        "balance_text": (
            "💰 Баланс\n"
            "Всего: {total} ₽\n"
            "Платный: {paid} ₽\n"
            "Бонусный: {bonus} ₽\n\n"
            "🆓 Бесплатно:\n"
            "• Чат: {chats} из {chats_max} — обновляется каждый день.\n"
            "• Картинки: {images} из {images_max} — один раз при регистрации.\n\n"
            "🔊 Пробные озвучки: {tts_free} — один раз при регистрации.\n\n"
            "Задачи и напоминания — при балансе. "
            "Первые 7 дней календарь и напоминания бесплатны."
        ),
        "bal_topup": "💰 Пополнить баланс",
        "bal_planner": "🗓 Планировщик Pro — календарь без ИИ",
        "bal_free_gift": "🎁 Получить баланс Бесплатно",
        "bal_how": "ℹ️ Как работает баланс",
        "bal_save": "💡 Как экономить баланс",
        "bal_spends_on": "✅ Показывать списания — выключить",
        "bal_spends_off": "🔕 Показывать списания — включить",
        "bal_close": "❌ Закрыть",
        "bal_back": "⬅️ Назад",
        "popular": "🔥 ПОПУЛЯРНОЕ",
        "topup_calc_title": "💰 На что хватит пакета (ИИ-баланс):",
        "topup_calc_line": "• {rub} ₽ ≈ {chats} запросов чата или {images} картинок low",
        "planner_title": (
            "🗓 Планировщик Pro\n\n"
            "Календарь и напоминания — без нейросетей.\n"
            "• {price} ₽ на {days} дней.\n"
            "• Осталось по подписке: {left} дн.\n\n"
            "Любой положительный ИИ-баланс тоже включает календарь. "
            "Планировщик Pro — если нейросети не нужны."
        ),
        "planner_done": (
            "✅ Планировщик Pro подключён на {days} дней!\n"
            "Календарь и напоминания снова доступны."
        ),
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
            "• Каждый день: {chats} чата.\n"
            "• Новым пользователям: {images} картинка, {tts} пробные озвучки "
            "и {voice} голосовых задач — всё один раз при регистрации.\n"
            "• Планировщик, задачи и календарь — всегда бесплатно."
        ),
        "how_balance_text": (
            "ℹ️ Как работает баланс — два продукта:\n\n"
            "🤖 ИИ-баланс — чат, картинки, озвучка.\n"
            "• Чат: цена считается по объёму запроса и ответа — "
            "от {p_chat_min} до {p_chat_max} ₽ за запрос.\n"
            "• Картинка: low {p_img_low} ₽, medium {p_img_med} ₽, high {p_img_high} ₽ — "
            "по качеству.\n"
            "• Озвучка: {p_tts} ₽ за 1000 знаков текста.\n"
            "• Бесплатно: 3 чата каждый день; новым — 1 картинка, "
            "3 озвучки и 5 голосовых задач при регистрации.\n\n"
            "🗓 Планировщик Pro — календарь и напоминания, без ИИ.\n"
            "• 99 ₽ на 30 дней; себестоимость≈0.\n"
            "• Первые 7 дней — бесплатно для всех.\n\n"
            "Любой положительный ИИ-баланс тоже открывает календарь."
        ),
        "save_balance_text": (
            "💡 Как экономить баланс:\n"
            "• Ловите дневную бесплатную норму: {chats} чата каждый день.\n"
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
        "spend_voice": "голосовая задача",
        "msg_too_long": (
            "📏 Запрос слишком длинный ({chars} знаков).\n"
            "Бесплатный лимит — {max} знаков. Сократите текст или "
            "пополните баланс для запросов любой длины."
        ),
        "short_mode_notice": (
            "⚡️ При балансе {balance} ₽ доступны только лаконичные ответы "
            "(до ~{chars} знаков).\n"
            "Если хотите развёрнутый ответ — пополните баланс."
        ),
        "voice_tasks_limit": (
            "🎙 Бесплатные голосовые задачи закончились.\n\n"
            "При регистрации включено 5 голосовых задач. Доп. голосовая задача — "
            "{price} ₽ с баланса. Текстом и в планировщике — без ограничений."
        ),
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
        "nav_back": "⬅️ Назад",
        "task_reminder_need_balance": (
            "⏰ Напоминания к задачам доступны при положительном балансе.\n"
            "Задача сохранена, но напоминание не установлено:"
        ),
        "task_reminder_need_balance_item": "• {task}",
        "planner_menu": (
            "📋 Задачи и календарь\n\nВыберите раздел:"
        ),
        "btn_my_tasks": "✅ Мои задачи",
        "nav_calendar_menu": "🗓 Календарь",
        "nav_back_menu": "⬅️ Назад в меню",
        "menu_returned": "🏠 Главное меню. Выберите действие на клавиатуре внизу.",
        "mytasks_hint": (
            "✍️ Наговорите или напишите свою задачу.\n\n"
            "Можно указать:\n"
            "• дату и время задачи (например, «завтра в 10:00»);\n"
            "• за сколько напомнить (например, «напомни за 15 минут»).\n\n"
            "⏰ Напоминания работают при положительном балансе. "
            "На бесплатном тарифе задачи можно записывать голосом или текстом, "
            "а напоминание подключить позже."
        ),
        "mytasks_hint_free_reminder": (
            "🔒 Напоминания доступны при положительном балансе. "
            "Задача сохранена без напоминания."
        ),
        "btn_goto_my_tasks": "📂 Перейти в мои задачи",
        "btn_open_task": "📄 Открыть",
        "btn_pay_reminder": "💳 Подключить напоминание",
        "task_folder_menu_title": "📂 Выберите папку:",
        "task_card_title": "📝 {title}",
        "task_card_due": "🕒 Срок: {date}",
        "task_card_reminder": "⏰ Напомню {value}",
        "task_card_reminder_off": "⏰ Напоминание не установлено",
        "task_done_btn": "✅ Сделано",
        "task_undone_btn": "↩️ Вернуть",
        "task_delete_btn": "🗑 Удалить",
        "task_delete_confirm_title": "🗑 Удалить задачу?",
        "task_delete_confirm_body": "«{title}» будет удалена безвозвратно. Точно удалить?",
        "task_delete_yes": "✅ Да, удалить",
        "task_delete_no": "↩️ Отмена",
        "task_remind_btn": "⏰ Напомнить",
        "task_remind_free_hint": (
            "⏰ Напоминание к задаче доступно при положительном балансе. "
            "Пополните баланс, чтобы подключить напоминание."
        ),
        "task_need_due_for_reminder": (
            "Чтобы поставить напоминание, укажите дату и время задачи "
            "(например, «завтра в 10:00»)."
        ),
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
        "task_reminder": "⏰ Task reminder: {title}\n📁 Folder: {category}",
        "reminder_options": "Reminders: {value}",
        "event_card": (
            "📅 Event: {title}\n"
            "🕒 {date}"
        ),
        "premium": "🎙 Free voice analyses left: {free}.",
        "not_admin": "This command is available to the administrator only.",
        "flood_warn": (
            "⏳ You are sending messages too quickly. "
            "Please wait a couple of seconds and try again."
        ),
        "grant_usage": "Format: `/add_free USER_ID COUNT`",
        "grant_ok": "✅ Added attempts: {count}.",
        "grant_user": "🎁 You received extra voice analyses: +{count}.",
        "support": "💬 For support, contact: {contact}",
        "unknown_timezone": "Unknown time zone. Use a valid IANA name, for example Europe/London.",
        "confirm_event": "Event saved.",
        "cal_title": "🗓 Calendar — {month}",
        "cal_legend": "• — days with plans. Tap a date to open it and add a plan.",
        "cal_no_dates": "No events this month.",
        "cal_day_title": "📅 {date}:",
        "cal_today": "📍 Today",
        "cal_back": "⬅️ Back to calendar",
        "cal_delete_btn": "🗑 Delete",
        "cal_delete_confirm_title": "🗑 Delete plan?",
        "cal_delete_confirm_body": "“{title}” will be deleted permanently. Delete it?",
        "cal_legend_new": (
            "🟢 — days with plans.\n"
            "📍 — the selected date.\n"
            "Tap “Choose a date”, then say or type an event or task."
        ),
        "cal_legend_trial": "🎁 Free calendar: {days} days left.",
        "cal_pick_date": "📍 Choose a date",
        "cal_pick_date_prompt": (
            "📍 Which date are we planning?\n\n"
            "Type the date as DD.MM.YYYY — for example, 25.10.2026.\n"
            "Or simply: “today”, “tomorrow”, “day after tomorrow”."
        ),
        "cal_pick_date_bad": (
            "Couldn’t recognize the date. Examples: 25.10.2026, tomorrow, today, day after tomorrow."
        ),
        "cal_date_set": (
            "✅ Date selected: {date}\n\n"
            "Now say or type what you’re planning."
        ),
        "cal_add_hint": (
            "✍️ Say or type an event or task for {date}.\n\n"
            "You can ask for a reminder right away — e.g. “meeting at 15:00, "
            "remind me 1 hour before”."
        ),
        "cal_day_empty": (
            "📭 Nothing is planned for this day yet.\n\n"
            "Say or type an event or task — and it will appear here."
        ),
        "cal_tap_day_hint": (
            "👇 Tap a date to open it and add a plan."
        ),
        "cal_day_reminders_title": "⏰ Reminders:",
        "cal_day_add_reminder": "⏰ Set a reminder",
        "cal_day_pick_format": (
            "⏰ How long before to remind you?\n\n"
            "Tap a ready-made option or type your own text, "
            "for example: “1 hour before”, “30 minutes before”, “2 days before”."
        ),
        "cal_day_locked": (
            "🔒 Reminders are available with a positive balance. "
            "Top up your balance to get reminders about your plans."
        ),
        "cal_day_selected_for_add": (
            "📅 Date selected: {date}.\n\n"
            "Now say or type an event or task for this date. "
            "You can ask for a reminder right away — e.g. “meeting at 15:00, "
            "remind me 1 hour before”."
        ),
        "cal_access_needed": (
            "🔒 The calendar with reminders is available with a positive balance.\n\n"
            "The first 7 days the calendar is free. Your trial period has ended — "
            "top up your balance to keep planning and receiving reminders."
        ),
        "cal_trial_welcome": (
            "🎁 Welcome to VoicePlan!\n\n"
            "We’re giving you a week of free access to the calendar and reminders — "
            "a full 7 days, starting today.\n\n"
            "During this time you can:\n"
            "• create events and tasks for any date;\n"
            "• get reminders ahead of time — 15 minutes, an hour, a day before, or your own time;\n"
            "• plan by voice or text right in the chat.\n\n"
            "After the 7 days the calendar and reminders remain available with a "
            "positive balance. Happy planning!"
        ),
        "cal_reminder_free_hint": (
            "⏰ Event reminders are enabled with a positive balance."
        ),
        "cal_reminder_added": "✅ Reminder added: {value}.",
        "cal_remind_change_btn": "🔄 Change reminder",
        "cal_remind_off_btn": "🚫 Remove reminder",
        "cal_reminder_current": "Current reminder: {value}. Pick a new one or remove it:",
        "evt_back": "⬅️ Back to event list",
        "cal_reminder_below": (
            "⏰ The button under a plan sets a reminder "
            "(e.g. 15 minutes or 1 hour before)."
        ),
        "free_usage": "Format: /free 10 — add attempts to yourself.\nWithout arguments — show balance.",
        "free_added": "🎁 Attempts added: +{count}. Balance: {free}.",
        "setbal_usage": (
            "Format: /setbalance <amount> [bonus]\n"
            "Examples: /setbalance 500 — paid balance 500, bonus 0\n"
            "/setbalance 0 — zero out the whole balance\n"
            "/setbalance 100 50 — paid 100, bonus 50\n"
            "Without arguments — show current balance."
        ),
        "setbal_done": "✅ Balance updated: paid {paid} RUB, bonus {bonus} RUB.",
        "btn_chat": "💬 AI Chat",
        "btn_image": "🎨 Images",
        "btn_video": "🎬 Video",
        "btn_oferta": "📖 Terms",
        "btn_exit_chat": "↩️ Exit chat",
        "btn_speak": "🔊 Speak aloud",
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
            "balance via Telegram Stars in “💎 Balance & pay”: the chat price "
            "depends on the size of the request and the answer, the image price "
            "— on quality, the voice reply price — on the text length (voicing "
            "the same text is charged once). No auto-renewal — "
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
        "free_paused": (
            "🛡 Free features are temporarily paused (protection mode).\n"
            "Topping up the balance will let you continue."
        ),
        "free_off_notice": (
            "⛔ Free features are not enabled for your account.\n"
            "Usage is available from your balance. Contact the admin with questions."
        ),
        "captcha_required": (
            "🛡 Registration activity is high right now — confirm you are human "
            "to receive the starter gifts.\n"
            "The button is valid for {sec} seconds. If it expires, send /start again."
        ),
        "captcha_btn": "✅ I'm not a robot",
        "captcha_toast_ok": "Confirmed!",
        "captcha_expired": "Expired — send /start again.",
        "captcha_done": "✅ Human verified.",
        "captcha_granted": (
            "✅ Thanks! Starter gifts granted: {chats} chats per day, "
            "{img} image, {voice} voice tasks, {tts} TTS tries."
        ),
        "captcha_needed": (
            "🤖 Please confirm you are not a robot: tap the "
            "“✅ I'm not a robot” button above. If it's missing — send /start."
        ),
        "guard_status_on": (
            "🛡 Protection mode: ON.\n"
            "Free gifts and daily quotas are paused; new users get no free "
            "allowance — only balances and Premium work.\n"
            "Total users: {total}, new today: {today} "
            "(daily registration cap with gifts: {cap}).\n"
            "Flood protection: auto-stop at {burst} signups per {window}s, "
            "captcha at {captcha} per window.\n"
            "Turn off: /guard off"
        ),
        "guard_status_off": (
            "🛡 Protection mode: OFF.\n"
            "Free gifts and quotas work as usual.\n"
            "Total users: {total}, new today: {today} "
            "(daily registration cap with gifts: {cap}).\n"
            "Flood protection: auto-stop at {burst} signups per {window}s, "
            "captcha at {captcha} per window.\n"
            "Turn on: /guard on"
        ),
        "bal_open": "💰 View balance",
        "balance_text": (
            "💰 Balance\n"
            "Total: {total} RUB\n"
            "Paid: {paid} RUB\n"
            "Bonus: {bonus} RUB\n\n"
            "🆓 Free:\n"
            "• Chat: {chats} of {chats_max} — refreshes every day.\n"
            "• Images: {images} of {images_max} — once on registration.\n\n"
            "🔊 Trial voice replies: {tts_free} — once on registration.\n\n"
            "Tasks and reminders — with a balance. "
            "The first 7 days the calendar and reminders are free."
        ),
        "bal_topup": "💰 Top up balance",
        "bal_planner": "🗓 Planner Pro — calendar without AI",
        "bal_free_gift": "🎁 Get balance for free",
        "bal_how": "ℹ️ How the balance works",
        "bal_save": "💡 How to save balance",
        "bal_spends_on": "✅ Show charges — turn off",
        "bal_spends_off": "🔕 Show charges — turn on",
        "bal_close": "❌ Close",
        "bal_back": "⬅️ Back",
        "popular": "🔥 POPULAR",
        "topup_calc_title": "💰 What a package buys you (AI balance):",
        "topup_calc_line": "• {rub} RUB ≈ {chats} chat requests or {images} low-quality images",
        "planner_title": (
            "🗓 Planner Pro\n\n"
            "Calendar and reminders — without AI.\n"
            "• {price} RUB for {days} days.\n"
            "• Days left: {left}.\n\n"
            "Any positive AI balance also enables the calendar. "
            "Planner Pro is for when you don't need AI."
        ),
        "planner_done": (
            "✅ Planner Pro is active for {days} days!\n"
            "Calendar and reminders are available again."
        ),
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
            "• Every day: {chats} chats.\n"
            "• New users: {images} image, {tts} trial voice replies and "
            "{voice} voice tasks — all once on registration.\n"
            "• Planner, tasks and calendar — always free."
        ),
        "how_balance_text": (
            "ℹ️ How it works — two products:\n\n"
            "🤖 AI balance — chat, images, voice.\n"
            "• Chat: the price depends on the size of your request and the answer — "
            "from {p_chat_min} to {p_chat_max} RUB per request.\n"
            "• Image: low {p_img_low} RUB, medium {p_img_med} RUB, high {p_img_high} RUB "
            "— by quality.\n"
            "• Voice: {p_tts} RUB per 1000 characters.\n"
            "• Free: 3 chats every day; new users get 1 image, "
            "3 voice replies and 5 voice tasks on registration.\n\n"
            "🗓 Planner Pro — calendar and reminders, no AI.\n"
            "• 99 RUB for 30 days; cost ≈ 0.\n"
            "• First 7 days free for everyone.\n\n"
            "Any positive AI balance also unlocks the calendar."
        ),
        "save_balance_text": (
            "💡 How to save your balance:\n"
            "• Use the daily free quota: {chats} chats every day.\n"
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
        "spend_voice": "voice task",
        "msg_too_long": (
            "📏 The request is too long ({chars} characters).\n"
            "The free limit is {max} characters. Shorten the text or "
            "top up your balance for requests of any length."
        ),
        "short_mode_notice": (
            "⚡️ With a {balance} RUB balance only short answers are available "
            "(up to ~{chars} characters).\n"
            "If you want a full answer — top up your balance."
        ),
        "voice_tasks_limit": (
            "🎙 Free voice tasks are used up.\n\n"
            "5 voice tasks are included on registration. An extra voice task "
            "costs {price} RUB from your balance. Text and planner are unlimited."
        ),
        "tts_locked": (
            "🔇 Trial voice replies are used up. Voice replies will turn on "
            "with a positive balance ({price} RUB/1000 characters)."
        ),
        "video_stub": "🎬 Video generation is coming very soon!",
        "yookassa_stub": "💳 YooKassa payments are not connected yet. Coming soon!",
        "btn_planner": "📋 Tasks & calendar",
        "task_reminder_need_balance": (
            "⏰ Task reminders are available with a positive balance.\n"
            "The task was saved, but no reminder was set:"
        ),
        "task_reminder_need_balance_item": "• {task}",
        "planner_menu": "📋 Tasks & calendar\n\nChoose a section:",
        "btn_my_tasks": "✅ My tasks",
        "nav_calendar_menu": "🗓 Calendar",
        "nav_back_menu": "⬅️ Back to menu",
        "menu_returned": "🏠 Main menu. Choose an action on the keyboard below.",
        "mytasks_hint": (
            "✍️ Record or type your task.\n\n"
            "You can specify:\n"
            "• task date and time (e.g. \"tomorrow at 10:00\");\n"
            "• how long before to remind (e.g. \"remind 15 minutes before\").\n\n"
            "⏰ Reminders work with a positive balance. On the free plan you can "
            "record tasks by voice or text and enable a reminder later."
        ),
        "mytasks_hint_free_reminder": (
            "🔒 Reminders are available with a positive balance. "
            "Task saved without a reminder."
        ),
        "btn_goto_my_tasks": "📂 Go to my tasks",
        "btn_open_task": "📄 Open",
        "btn_pay_reminder": "💳 Enable reminder",
        "task_folder_menu_title": "📂 Choose a folder:",
        "task_card_title": "📝 {title}",
        "task_card_due": "🕒 Due: {date}",
        "task_card_reminder": "⏰ Remind {value}",
        "task_card_reminder_off": "⏰ No reminder set",
        "task_done_btn": "✅ Done",
        "task_undone_btn": "↩️ Undo",
        "task_delete_btn": "🗑 Delete",
        "task_delete_confirm_title": "🗑 Delete task?",
        "task_delete_confirm_body": "“{title}” will be deleted permanently. Delete it?",
        "task_delete_yes": "✅ Yes, delete",
        "task_delete_no": "↩️ Cancel",
        "task_remind_btn": "⏰ Remind",
        "task_remind_free_hint": (
            "⏰ Task reminders are available with a positive balance. "
            "Top up your balance to enable a reminder."
        ),
        "task_need_due_for_reminder": (
            "To set a reminder, specify the task date and time "
            "(e.g. \"tomorrow at 10:00\")."
        ),
        "nav_folders": "📂 Folders",
        "nav_calendar": "🗓 Calendar",
        "nav_reminders": "⏰ Reminders",
        "nav_events": "📅 Events",
        "nav_newcat": "➕ New folder",
        "nav_tasks": "⬅️ To tasks",
        "nav_back": "⬅️ Back",
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
    "ru": {"15": "за 15 минут", "30": "за 30 минут", "60": "за 1 час"},
    "en": {"15": "15 min before", "30": "30 min before", "60": "1 hour before"},
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
                    ui_mode TEXT DEFAULT '',
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
                "daily_image_left": f"INTEGER DEFAULT {FREE_IMAGES_ONCE}",
                "daily_voice_left": f"INTEGER DEFAULT {FREE_VOICE_TASKS_ONCE}",
                "show_spends": "INTEGER DEFAULT 1",
                # Планировщик Pro: дата (ISO UTC) до которой открыт календарь
                # и напоминания без ИИ-баланса.
                "planner_until": "TEXT",
                # Долгоживущий режим интерфейса (чат / картинки). Хранится в БД,
                # чтобы не теряться при перезапуске бота (Render засыпает/просыпается,
                # локальный перезапуск). Временные состояния (категория, часовой пояс,
                # своё время напоминания) по-прежнему живут в user_states.
                "ui_mode": "TEXT DEFAULT ''",
                # 7-дневный бесплатный доступ к календарю для новых пользователей:
                # trial_started_at фиксирует момент старта триала (однократно),
                # calendar_free_ack — чтобы приветственное сообщение о триале
                # показывалось только один раз.
                "trial_started_at": "TEXT",
                "calendar_free_ack": "INTEGER DEFAULT 0",
                # Аккаунт «без бесплатки»: создан в guard-режиме или сверх
                # дневного лимита регистраций — без подарков и дневных норм.
                "free_off": "INTEGER DEFAULT 0",
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
                    due_utc TEXT,
                    remind_before_min INTEGER,
                    remind_utc TEXT,
                    remind_sent INTEGER DEFAULT 0,
                    timezone TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Миграция существующей таблицы tasks без потери данных.
            tcols = table_columns(conn, "tasks")
            task_migrations = {
                "due_utc": "TEXT",
                "remind_before_min": "INTEGER",
                "remind_utc": "TEXT",
                "remind_sent": "INTEGER DEFAULT 0",
                "timezone": "TEXT",
            }
            for column, definition in task_migrations.items():
                if column not in tcols:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")

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
            # Кэш озвученных ответов: повторное прослушивание переотправляется
            # по file_id без вызова TTS-API и без списаний с баланса.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tts_cache (
                    user_id INTEGER PRIMARY KEY,
                    answer_hash TEXT NOT NULL DEFAULT '',
                    voice_file_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Служебные флаги бота: режим guard, запомненные вехи роста.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # created_at: у баз, мигрированных ALTER TABLE (колонка без
            # DEFAULT CURRENT_TIMESTAMP), проставляем время вставки триггером —
            # иначе новые пользователи не попадают в отчёт и в дневной лимит
            # регистраций.
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_users_created_at
                AFTER INSERT ON users
                WHEN NEW.created_at IS NULL
                BEGIN
                    UPDATE users SET created_at = CURRENT_TIMESTAMP
                    WHERE user_id = NEW.user_id;
                END
                """
            )
            migrate_reminder_10_to_15(conn)
            cleanup_legacy_folders(conn)
            conn.commit()
        finally:
            conn.close()


def cleanup_legacy_folders(conn):
    """Идемпотентная уборка устаревших пользовательских папок.

    Папки «Учёба» и «Покупки» были созданы предыдущей версией бота. Пользователь
    попросил их убрать (при необходимости создаст свои). Но задачи из этих и других
    «осиротевших» категорий (чьи папки больше не существуют) нельзя терять — поэтому
    переносим такие задачи в папку по умолчанию («📋 Повседневное» / «📋 Daily»),
    соответствующую языку пользователя. Папки удаляем только по точному имени.
    """
    legacy_names = {"👩‍🎓 Учеба", "🛍 Покупки"}

    # 1) Удаляем только две указанные устаревшие папки (по всем пользователям).
    for name in legacy_names:
        conn.execute(
            "DELETE FROM custom_categories WHERE category_name = ?", (name,)
        )

    # 2) Переносим задачи из «несуществующих» категорий в папку по умолчанию,
    #    чтобы они не пропали из интерфейса. Валидные категории = системные
    #    папки языка пользователя + оставшиеся пользовательские папки.
    users = conn.execute("SELECT user_id, language FROM users").fetchall()
    for user in users:
        uid = user["user_id"]
        lang = user["language"] if user["language"] in SYSTEM_CATEGORIES else "ru"
        valid = set(SYSTEM_CATEGORIES[lang])
        custom = conn.execute(
            "SELECT category_name FROM custom_categories WHERE user_id = ?", (uid,)
        ).fetchall()
        valid.update(row["category_name"] for row in custom)

        default_folder = SYSTEM_CATEGORIES[lang][1]  # «📋 Повседневное» / «📋 Daily»
        orphaned = conn.execute(
            "SELECT DISTINCT category FROM tasks WHERE user_id = ?", (uid,)
        ).fetchall()
        for row in orphaned:
            if row["category"] not in valid:
                conn.execute(
                    "UPDATE tasks SET category = ? WHERE user_id = ? AND category = ?",
                    (default_folder, uid, row["category"]),
                )


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


def get_ui_mode(uid):
    """Долгоживущий режим интерфейса из БД: 'chat' / 'image_prompt' / '' (нет)."""
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT ui_mode FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            return (row["ui_mode"] or "") if row else ""
        finally:
            conn.close()


def set_ui_mode(uid, mode):
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE users SET ui_mode = ? WHERE user_id = ?", (mode or "", uid)
            )
            conn.commit()
        finally:
            conn.close()


def ui_mode(uid):
    """chat / image_prompt / '' — приоритет памяти процесса, фолбэк на БД.

    Временные состояния ввода (category, timezone, custom_reminder:<id>)
    отключают долгий режим чата/картинок, пока пользователь что-то вводит.
    Память быстра в пределах сессии; БД — страховка при перезапуске бота.
    """
    mem = user_states.get(uid)
    if isinstance(mem, str):
        if mem in ("chat", "image_prompt"):
            return mem
        # Любой другой ввод (папка, часовой пояс, своё время) — не режим чата.
        return ""
    return get_ui_mode(uid)


def enter_mode(uid, mode):
    """Войти в режим интерфейса (chat / image_prompt) — память + БД."""
    user_states[uid] = mode
    set_ui_mode(uid, mode)


def leave_mode(uid):
    """Выйти из режима интерфейса — чистим и память, и БД."""
    user_states.pop(uid, None)
    set_ui_mode(uid, "")


def begin_input(uid, state):
    """Начать временный ввод (категория/часовой пояс/своё время).

    Сбрасывает долгий режим чата/картинок и в БД тоже, иначе после ввода
    чат неожиданно «включился» бы обратно из сохранённого ui_mode.
    """
    set_ui_mode(uid, "")
    user_states[uid] = state


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


# --- Антифлуд: скользящее окно на пользователя -------------------------------
# Хранилище: uid -> список монотонных меток времени сообщений за текущее окно.
# Записи старше окна выкидываются при каждом обращении, а простаивающие
# пользователи удаляются целиком, поэтому словарь не растёт бесконечно
# (в отличие от учебного примера со словарём без чистки).
_flood_lock = threading.Lock()
_flood_hits = {}       # uid -> [метки time.monotonic() за окно]
_flood_warned = {}     # uid -> метка последнего предупреждения о флуде


def flood_gate(message):
    """True — можно обрабатывать сообщение; False — флуд (сообщение игнорируется).

    Скользящее окно: не более FLOOD_MAX_EVENTS сообщений за FLOOD_WINDOW_SEC
    секунд. При срабатывании шлётся одно предупреждение на окно, чтобы сам
    ответ бота не превращался в флуд. Админ (ADMIN_ID) не ограничивается.
    """
    uid = message.from_user.id
    if uid == ADMIN_ID:
        return True
    now = time.monotonic()
    cutoff = now - FLOOD_WINDOW_SEC
    with _flood_lock:
        # Чистка простаивших пользователей — защита от утечки памяти.
        stale = [u for u, hits in _flood_hits.items() if not hits or hits[-1] <= cutoff]
        for u in stale:
            del _flood_hits[u]
            if u in _flood_warned:
                del _flood_warned[u]
        hits = _flood_hits.setdefault(uid, [])
        # Выкидываем метки, вышедшие из скользящего окна (кроме новой).
        hits[:] = [t for t in hits if t > cutoff]
        hits.append(now)
        blocked = len(hits) > FLOOD_MAX_EVENTS
        # Предупреждаем не чаще одного раза на окно.
        warn = False
        if now - _flood_warned.get(uid, 0.0) >= FLOOD_WINDOW_SEC:
            _flood_warned.pop(uid, None)
        if blocked and uid not in _flood_warned:
            _flood_warned[uid] = now
            warn = True
    if blocked:
        logger.warning("Антифлуд: uid=%s превысил лимит %s сообщений за %s с",
                       uid, FLOOD_MAX_EVENTS, FLOOD_WINDOW_SEC)
        if warn:
            try:
                bot.reply_to(message, tr(uid, "flood_warn"))
            except Exception:
                logger.exception("Не удалось отправить предупреждение о флуде")
        return False
    return True


def get_voice_quota(uid):
    """(daily_voice_left, is_premium) — разовая квота при регистрации.

    Квота НЕ сбрасывается ежедневно: 5 бесплатных голосовых задач
    выдаются один раз (daily_voice_left хранит остаток).
    """
    ensure_daily(uid)
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT daily_voice_left, is_premium FROM users WHERE user_id = ?",
                (uid,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return 0, 0
    return int(row["daily_voice_left"] or 0), int(row["is_premium"] or 0)


def try_consume_voice_task(uid):
    """Проверить и списать возможность голосовой задачи.

    Порядок: Premium (легаси) безлимит -> дневная квота (5/день)
    -> доп. голос с баланса (VOICE_TASK_PRICE_RUB). Возвращает (ок?, причина).
    """
    ensure_daily(uid)
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            row = conn.execute(
                "SELECT daily_voice_left, is_premium "
                "FROM users WHERE user_id = ?", (uid,),
            ).fetchone()
            if row and row["is_premium"]:
                return True, "premium"
            # Guard-режим: дневная квота приостановлена — только баланс.
            if row and int(row["daily_voice_left"] or 0) > 0 and not guard_on():
                conn.execute(
                    "UPDATE users SET daily_voice_left = daily_voice_left - 1 WHERE user_id = ?",
                    (uid,),
                )
                conn.commit()
                return True, "daily"
        finally:
            conn.close()
    # Дневная квота исчерпана — доп. голосовые с баланса.
    if spend_from_balance(uid, VOICE_TASK_PRICE_RUB):
        maybe_receipt(uid, VOICE_TASK_PRICE_RUB, "spend_voice")
        return True, "paid"
    return False, "blocked"


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
# ЗАЩИТА БЕСПЛАТКИ: режим guard, вехи роста, дневной лимит регистраций.
# ------------------------------------------------------------

def meta_get_locked(conn, key, default=""):
    """Прочитать служебный флаг из bot_meta (вызов внутри открытого conn)."""
    row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set_locked(conn, key, value):
    """Записать служебный флаг в bot_meta (вызов внутри открытого conn)."""
    conn.execute(
        "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def guard_on():
    """True — включён режим защиты: бесплатные бонусы приостановлены."""
    with db_lock:
        conn = get_db()
        try:
            return meta_get_locked(conn, "guard_mode") == "1"
        finally:
            conn.close()


def set_guard(enabled):
    """Включить/выключить режим защиты (админ-команда /guard on|off)."""
    with db_lock:
        conn = get_db()
        try:
            meta_set_locked(conn, "guard_mode", "1" if enabled else "0")
            conn.commit()
        finally:
            conn.close()


def free_off(uid):
    """Аккаунт без бесплатных норм: создан в guard-режиме либо сверх дневного
    лимита регистраций. Такой пользователь работает только с баланса."""
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT free_off FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            return bool(row and row["free_off"])
        finally:
            conn.close()


def _new_users_today_locked(conn):
    """Сколько аккаунтов создано сегодня (UTC — как CURRENT_TIMESTAMP в БД)."""
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
    return int(
        conn.execute(
            "SELECT COUNT(*) n FROM users WHERE created_at >= ?", (since,)
        ).fetchone()["n"]
    )


def _apply_new_user_policy_locked(conn, uid):
    """Политика для СВЕЖЕГО аккаунта: в guard-режиме или при превышении
    дневного лимита регистраций — без подарков и без дневных норм (free_off=1).
    Возвращает True, если аккаунт помечен как «без бесплатки».
    """
    if not guard_on() and _new_users_today_locked(conn) <= MAX_NEW_USERS_PER_DAY:
        return False
    conn.execute(
        """UPDATE users SET free_off = 1,
                  daily_chat_left = 0, daily_image_left = 0,
                  daily_voice_left = 0, tts_free_left = 0, free_voice_left = 0
           WHERE user_id = ?""",
        (uid,),
    )
    return True


def free_blocked_key(uid):
    """Ключ текста отказа, когда бесплатка недоступна: guard → «пауза»,
    капча не пройдена → «подтвердитесь», free_off → «норм нет» (навсегда),
    иначе привычное «квоты на сегодня»."""
    if guard_on():
        return "free_paused"
    if captcha_pending(uid):
        return "captcha_needed"
    return "free_off_notice" if free_off(uid) else "tries_limit"


def _signups_in_window_locked(conn, window):
    """Сколько аккаунтов создано за последние window секунд (UTC)."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=window)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return int(
        conn.execute(
            "SELECT COUNT(*) n FROM users WHERE created_at >= ?", (since,)
        ).fetchone()["n"]
    )


def _detect_signup_burst_locked(conn):
    """Флуд-детект регистраций (A): превышение SIGNUP_BURST_LIMIT за окно →
    guard включается автоматически, кулдаун капчи продлевается, админу
    ставится флаг оповещения (отправляется снаружи, с повтором при неудаче)."""
    if _signups_in_window_locked(conn, SIGNUP_BURST_WINDOW) <= SIGNUP_BURST_LIMIT:
        return
    meta_set_locked(conn, "burst_at", str(int(time.time())))
    if meta_get_locked(conn, "guard_mode") == "1":
        return  # guard уже включён (вручную или раньше) — лишь продлеваем кулдаун
    meta_set_locked(conn, "guard_mode", "1")
    meta_set_locked(conn, "burst_alert", "1")


def _captcha_required_locked(conn):
    """Капча «Я не робот» для НОВЫХ аккаунтов (B): всплеск выше мягкого
    порога либо кулдаун после автостопа (SIGNUP_CAPTCHA_TTL секунд)."""
    if _signups_in_window_locked(conn, SIGNUP_BURST_WINDOW) > SIGNUP_CAPTCHA_LIMIT:
        return True
    raw = meta_get_locked(conn, "burst_at", "")
    return raw.isdigit() and int(time.time()) - int(raw) < SIGNUP_CAPTCHA_TTL


def _fresh_signup_locked(conn, uid):
    """Свежий ID (C-lite): True — аккаунт моложе всех ранее виденных ботом
    (ID Telegram монотонны ⇒ «одноразовый свежесозданный»). Граница uid_hi
    инициализируется по истории (MAX(user_id)) и после каждого новичка
    поднимается до него — каждый следующий расходник атакующого снова «свежий».
    При SIGNUP_FRESH_CAPTCHA=0 граница копится, но капча не ставится."""
    raw = meta_get_locked(conn, "uid_hi", "")
    if raw.isdigit():
        hi = int(raw)
    else:
        row = conn.execute(
            "SELECT MAX(user_id) m FROM users WHERE user_id <> ?", (uid,)
        ).fetchone()
        hi = int(row["m"] or 0)
        meta_set_locked(conn, "uid_hi", str(hi))
    if uid > hi:
        meta_set_locked(conn, "uid_hi", str(uid))
        return SIGNUP_FRESH_CAPTCHA
    return False


def _captcha_key_locked(uid):
    return f"captcha:{uid}"


def captcha_pending(uid):
    """True — аккаунт ждёт нажатия «Я не робот» (токен выдан и не погашен)."""
    with db_lock:
        conn = get_db()
        try:
            return bool(meta_get_locked(conn, _captcha_key_locked(uid), ""))
        finally:
            conn.close()


def _hold_for_captcha_locked(conn, uid):
    """Заморозить подарки аккаунта до нажатия «Я не робот»: free_off + нулевые
    квоты (как в guard), одноразовый токен в bot_meta. Возвращает nonce."""
    nonce = os.urandom(4).hex()
    exp = int(time.time()) + SIGNUP_CAPTCHA_TIMEOUT
    meta_set_locked(conn, _captcha_key_locked(uid), f"{exp}:{nonce}")
    conn.execute(
        """UPDATE users SET free_off = 1,
                  daily_chat_left = 0, daily_image_left = 0,
                  daily_voice_left = 0, tts_free_left = 0, free_voice_left = 0
           WHERE user_id = ?""",
        (uid,),
    )
    return nonce


def captcha_markup(uid, nonce):
    """Кнопка «Я не робот»: callback rc:<uid>:<nonce>."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "captcha_btn"), callback_data=f"rc:{uid}:{nonce}"
        )
    )
    return markup


def try_send_burst_alert():
    """Оповестить админа о всплеске. Флаг лежит в БД: guard включается до
    отправки, поэтому при отсутствии интернета попытка повторится со
    следующей регистрацией (как и с вехами роста)."""
    if not ADMIN_ID:
        return
    with db_lock:
        conn = get_db()
        try:
            if meta_get_locked(conn, "burst_alert") != "1":
                return
            n = _signups_in_window_locked(conn, SIGNUP_BURST_WINDOW)
        finally:
            conn.close()
    try:
        bot.send_message(
            ADMIN_ID,
            f"🚨 Всплеск регистраций: {n} за {SIGNUP_BURST_WINDOW} сек — "
            f"бесплатка остановлена автоматически (guard ON).\n"
            f"Если наплыв легальный — /guard off.\n"
            f"Новые аккаунты ещё {SIGNUP_CAPTCHA_TTL // 60} мин подтверждают "
            f"«Я не робот».",
        )
    except Exception:
        logger.exception("Не удалось отправить оповещение о всплеске регистраций")
        return
    with db_lock:
        conn = get_db()
        try:
            meta_set_locked(conn, "burst_alert", "0")
            conn.commit()
        finally:
            conn.close()


def check_user_milestone():
    """Уведомить админа при пересечении вехы роста (USER_MILESTONES).

    «Пользователей стало N — пора пересмотреть условия бесплатки». Веха
    запоминается ТОЛЬКО после успешной отправки: если интернета нет,
    попытка повторится со следующей регистрацией.
    """
    if not ADMIN_ID:
        return
    with db_lock:
        conn = get_db()
        try:
            total = int(conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"])
            raw = meta_get_locked(conn, "user_milestone", "0") or "0"
            try:
                last = int(raw)
            except ValueError:
                last = 0
            crossed = max(
                (m for m in USER_MILESTONES if m > last and total >= m), default=0
            )
        finally:
            conn.close()
    if not crossed:
        return
    try:
        bot.send_message(
            ADMIN_ID,
            f"⚠️ Пользователей: {total} — превышена веха {crossed} чел.\n"
            f"Необходимо обновить условия бесплатных возможностей.\n"
            f"Быстрый стоп бесплатки без деплоя: /guard on",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление о вехе роста")
        return
    with db_lock:
        conn = get_db()
        try:
            meta_set_locked(conn, "user_milestone", str(crossed))
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
            cur = conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            if cur.rowcount:
                # Строка только что создана — политика бесплатки:
                # guard / дневной лимит регистраций → аккаунт без норм.
                _apply_new_user_policy_locked(conn, uid)
            row = conn.execute(
                "SELECT free_day, free_off FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if not row or (row["free_day"] or "") != today:
                # Сбрасывается только дневная чат-норма. Картинки и голосовые
                # задачи — разовые (выдаются при регистрации) и не обновляются.
                # Guard-режим и free_off → бесплатная норма не выдаётся вовсе.
                no_free = bool(row and row["free_off"]) or meta_get_locked(
                    conn, "guard_mode"
                ) == "1"
                conn.execute(
                    "UPDATE users SET free_day = ?, daily_chat_left = ? "
                    "WHERE user_id = ?",
                    (today, 0 if no_free else FREE_CHATS_PER_DAY, uid),
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
                    FREE_CHATS_PER_DAY, FREE_IMAGES_ONCE, 1,
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


TRIAL_DAYS = 7


def start_calendar_trial(uid):
    """Однократно фиксирует старт 7-дневного бесплатного доступа к календарю.

    Вызывается при регистрации. Возвращает True, если триал только что стартовал
    (чтобы показать приветственное сообщение ровно один раз).
    """
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            row = conn.execute(
                "SELECT trial_started_at FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if row and not row["trial_started_at"]:
                conn.execute(
                    "UPDATE users SET trial_started_at=? WHERE user_id=?",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), uid),
                )
                conn.commit()
                return True
            return False
        finally:
            conn.close()


def calendar_trial_days_left(uid):
    """Сколько дней бесплатного триала осталось (0 — триал закончился/не начат)."""
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT trial_started_at FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
        finally:
            conn.close()
    if not row or not row["trial_started_at"]:
        return 0
    try:
        started = datetime.fromisoformat(row["trial_started_at"])
    except (ValueError, TypeError):
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 86400
    left = TRIAL_DAYS - elapsed
    return max(0, int(left) if left > 0 else 0)


def planner_until(uid):
    """ISO-дата окончания Планировщика Pro или None."""
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT planner_until FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
        finally:
            conn.close()
    if not row or not row["planner_until"]:
        return None
    try:
        dt = datetime.fromisoformat(row["planner_until"])
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def planner_active(uid):
    """Планировщик Pro ещё действует?"""
    dt = planner_until(uid)
    return bool(dt and dt > datetime.now(timezone.utc))


def calendar_access(uid):
    """Доступ к календарю: (доступен?, причина).

    Доступ есть при положительном ИИ-балансе, при активном Планировщике Pro
    или во время 7-дневного триала для новых пользователей.
    Причина: 'paid' | 'planner' | 'trial' | 'expired'.
    """
    if has_paid_balance(uid):
        return True, "paid"
    if planner_active(uid):
        return True, "planner"
    days_left = calendar_trial_days_left(uid)
    if days_left > 0:
        return True, "trial"
    return False, "expired"


def _calendar_locked(chat_id, uid):
    """Календарь закрыт: триал закончился и баланса нет."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
    bot.send_message(chat_id, tr(uid, "cal_access_needed"), reply_markup=markup)


# Выбранная пользователем дата календаря (в памяти процесса): {uid: date}.
# Нужна, чтобы следующее наговоренное/написанное сообщение попало на эту дату.
selected_cal_date = {}


def _maybe_show_trial_welcome(chat_id, uid):
    """Показать приветствие о 7 бесплатных днях ровно один раз (флаг в БД).

    Возвращает True, если сообщение было отправлено.
    """
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            row = conn.execute(
                "SELECT calendar_free_ack FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if row is not None and row["calendar_free_ack"]:
                return False
            conn.execute(
                "UPDATE users SET calendar_free_ack=1 WHERE user_id=?", (uid,)
            )
            conn.commit()
        finally:
            conn.close()
    try:
        bot.send_message(chat_id, tr(uid, "cal_trial_welcome"))
    except Exception:
        logger.exception("Не удалось отправить приветствие о бесплатном календаре")
    return True


def parse_user_date(text):
    """Разбор даты из текста → date или None.

    Понимает: ДД.ММ.ГГГГ, ДД.ММ, ГГГГ-ММ-ДД, «сегодня», «завтра», «послезавтра».
    """
    import re

    raw = (text or "").strip().lower().replace("ё", "е")
    if not raw:
        return None
    # Относительные даты.
    if raw in {"сегодня", "today"}:
        return date.today()
    if raw in {"завтра", "tomorrow"}:
        return date.today() + timedelta(days=1)
    if raw in {"послезавтра", "после завтра", "day after tomorrow"}:
        return date.today() + timedelta(days=2)

    match = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", raw)
    if match:
        day, month, year = (int(x) for x in match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})", raw)
    if match:
        day, month = (int(x) for x in match.groups())
        year = date.today().year
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        year, month, day = (int(x) for x in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None


def _now_in_user_tz_local(uid):
    tz_name, _off, _voice = get_user_preferences(uid)
    try:
        return datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc)


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


def chat_price_rub(prompt_tokens, completion_tokens):
    """Цена чата для пользователя: по фактическим токенам × AI_MARKUP.

    Как у UnlimAI: короткий вопрос дешёвый, длинный — дороже. Цена ограничена
    минималкой (CHAT_MIN_RUB) и потолком (CHAT_MAX_RUB), округление до копеек.
    """
    raw = (float(prompt_tokens or 0) / 1_000_000) * LLM_PRICE_IN_RUB
    raw += (float(completion_tokens or 0) / 1_000_000) * LLM_PRICE_OUT_RUB
    price = raw * AI_MARKUP
    return round(min(max(price, CHAT_MIN_RUB), CHAT_MAX_RUB), 2)


def image_price_rub(quality=None):
    """Цена картинки для пользователя по качеству (по умолчанию — текущее)."""
    return IMAGE_PRICES_RUB.get(quality or IMAGE_QUALITY, IMAGE_PRICES_RUB["low"])


def tts_price_rub(text):
    """Цена озвучки текста: PRICE_TTS_RUB_PER_1K за 1000 знаков (лимит TTS-модели)."""
    return round(len(text[:3500]) / 1000 * PRICE_TTS_RUB_PER_1K, 3)


def _tts_answer_hash(text):
    """Хэш озвученного ответа: повторное прослушивание того же текста бесплатно."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def get_tts_cache(uid):
    """(answer_hash, voice_file_id) последней озвучки или (None, None)."""
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT answer_hash, voice_file_id FROM tts_cache WHERE user_id = ?",
                (uid,),
            ).fetchone()
            return (row["answer_hash"], row["voice_file_id"]) if row else (None, None)
        finally:
            conn.close()


def set_tts_cache(uid, answer_hash, voice_file_id):
    """Запомнить озвученный ответ: при повторе переотправим file_id без TTS-API."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO tts_cache (user_id, answer_hash, voice_file_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       answer_hash=excluded.answer_hash,
                       voice_file_id=excluded.voice_file_id,
                       updated_at=CURRENT_TIMESTAMP""",
                (uid, answer_hash, voice_file_id),
            )
            conn.commit()
        finally:
            conn.close()


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

    # Новому пользователю запускаем 7-дневный бесплатный доступ к календарю.
    trial_started = start_calendar_trial(uid) if is_new else False

    if is_new:
        # Порядок: флуд-детект (сам включает guard при всплеске) → политика
        # бесплатки → капча «Я не робот»: при всплеске/кулдауне ИЛИ для свежего
        # ID (аккаунт моложе всех виденных — одноразовый, см. _fresh_signup_locked).
        # (Здесь, а не в ensure_daily: строка users создаётся именно при /start.)
        with db_lock:
            conn = get_db()
            try:
                _detect_signup_burst_locked(conn)
                _apply_new_user_policy_locked(conn, uid)
                row = conn.execute(
                    "SELECT free_off FROM users WHERE user_id = ?", (uid,)
                ).fetchone()
                fresh = _fresh_signup_locked(conn, uid)
                if not (row and row["free_off"]) and (
                    fresh or _captcha_required_locked(conn)
                ):
                    _hold_for_captcha_locked(conn, uid)
                conn.commit()
            finally:
                conn.close()
        # Веха роста: «пользователей уже N — пересмотрите условия бесплатки».
        # Уведомление уходит до запоминания вехи: при ошибке отправки
        # (нет интернета) попытка повторится со следующей регистрацией.
        check_user_milestone()
        # Оповещение о всплеске: guard уже включён в БД, отправка — с повтором.
        try_send_burst_alert()
    else:
        # Повторный /start при непройденной капче — продлеваем токен,
        # чтобы кнопка снова стала действительна.
        with db_lock:
            conn = get_db()
            try:
                if meta_get_locked(conn, _captcha_key_locked(uid)):
                    _hold_for_captcha_locked(conn, uid)
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

    # Капча для нового/неподтверждённого аккаунта во время всплеска.
    with db_lock:
        conn = get_db()
        try:
            raw = meta_get_locked(conn, _captcha_key_locked(uid), "")
        finally:
            conn.close()
    if raw:
        nonce = raw.partition(":")[2]
        try:
            bot.send_message(
                message.chat.id,
                tr(uid, "captcha_required", sec=SIGNUP_CAPTCHA_TIMEOUT),
                reply_markup=captcha_markup(uid, nonce),
            )
        except Exception:
            logger.exception("Не удалось отправить капчу пользователю")

    return trial_started


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
        images_max=FREE_IMAGES_ONCE,
        tts_free=tts_free,
    )
    spends_label = tr(uid, "bal_spends_on") if show else tr(uid, "bal_spends_off")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"),
        types.InlineKeyboardButton(tr(uid, "bal_planner"), callback_data="bal:planner"),
        types.InlineKeyboardButton(tr(uid, "bal_how"), callback_data="bal:how"),
        types.InlineKeyboardButton(tr(uid, "bal_save"), callback_data="bal:save"),
        types.InlineKeyboardButton(spends_label, callback_data="bal:spends"),
        types.InlineKeyboardButton(tr(uid, "bal_close"), callback_data="bal:close"),
    )
    return text, markup


def _balance_calc(uid):
    """Калькулятор: на что хватит пакета (чаты по типичной цене / картинки low)."""
    lines = [tr(uid, "topup_calc_title")]
    img_low = image_price_rub("low")
    for rub, _pct in TOPUP_PACKAGES:
        chats = int(rub // CHAT_TYPICAL_RUB) if CHAT_TYPICAL_RUB else 0
        images = int(rub // img_low) if img_low else 0
        lines.append(
            tr(uid, "topup_calc_line", rub=rub, chats=chats, images=images)
        )
    return "\n".join(lines)


def planner_screen(uid):
    """Экран «🗓 Планировщик Pro»: календарь и напоминания без ИИ."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        f"⭐ {PLANNER_PRICE_RUB} XTR — {PLANNER_DAYS} дн.",
        callback_data="bal:planner_buy",
    ))
    markup.add(types.InlineKeyboardButton(tr(uid, "bal_back"), callback_data="bal:show"))
    markup.add(types.InlineKeyboardButton("💳 ЮKassa", callback_data="pay_yookassa"))
    until = planner_until(uid)
    days = 0
    if until:
        left = (until - datetime.now(timezone.utc)).total_seconds() / 86400
        days = max(0, int(left) if left > 0 else 0)
    return tr(uid, "planner_title", price=PLANNER_PRICE_RUB, days=PLANNER_DAYS, left=days), markup


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
    return tr(uid, "topup_title") + "\n\n" + _balance_calc(uid), markup


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
    trial_started = register_user(message)
    leave_mode(message.from_user.id)

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

    # Приветственное сообщение о 7 бесплатных днях календаря — ровно один раз.
    if trial_started and _maybe_show_trial_welcome(message.chat.id, message.from_user.id):
        pass


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
    """Меню раздела «Задачи и календарь»: Мои задачи, Календарь, Назад в меню.

    Озвучка ответов живёт в чате с ИИ (кнопка «🔊 Озвучить» под ответом).
    """
    markup.row(
        types.InlineKeyboardButton(tr(uid, "btn_my_tasks"), callback_data="planner:menu_tasks"),
    )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_calendar_menu"), callback_data="planner:menu_calendar"),
    )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_back_menu"), callback_data="menu:main"),
    )
    return markup


def planner_menu(uid):
    """Главное меню раздела «Задачи и календарь»."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    _planner_nav(uid, markup)
    return tr(uid, "planner_menu"), markup


@bot.message_handler(commands=["tasks"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_planner"], LANGS["en"]["btn_planner"]
})
def show_tasks(message):
    text, markup = planner_menu(message.from_user.id)
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
    access_ok, _reason = calendar_access(uid)

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

        lines.append(tr(uid, "event_card", title=row["title"], date=date_text))
        for minutes in sorted({int(x) for x in offsets}):
            lines.append(f"   ⏰ {_offset_label(uid, minutes)}")
        # Кнопка напоминания: поставить (нет напоминания) или изменить/убрать (есть).
        if access_ok:
            if offsets:
                markup.add(
                    types.InlineKeyboardButton(
                        f"{tr(uid, 'cal_remind_change_btn')} {row['title'][:16]}",
                        callback_data=f"evtpick:{row['event_id']}",
                    )
                )
            else:
                markup.add(
                    types.InlineKeyboardButton(
                        f"⏰ {row['title'][:24]}",
                        callback_data=f"evtpick:{row['event_id']}",
                    )
                )
        markup.add(
            types.InlineKeyboardButton(
                f"🗑 {row['title'][:30]}",
                callback_data=f"eventdel:{row['event_id']}",
            )
        )

    # Компактная навигация внизу списка: календарь и задачи всегда под рукой.
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_calendar_menu"), callback_data="planner:menu_calendar"),
        types.InlineKeyboardButton(tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"),
    )

    return "\n\n".join(lines), markup


@bot.message_handler(commands=["events"])
@bot.message_handler(func=lambda m: bool(m.text) and m.text in {
    LANGS["ru"]["btn_events"], LANGS["en"]["btn_events"]
})
def show_events(message):
    text, markup = upcoming_events_text(message.from_user.id)
    bot.send_message(message.chat.id, text, reply_markup=markup)


def folders_view(uid):
    """Список папок пользователя (все) + «Новая папка» + назад к задачам/меню."""
    categories = get_user_categories(uid)
    text = tr(uid, "task_folder_menu_title")

    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, category in enumerate(categories):
        markup.add(
            types.InlineKeyboardButton(
                f"📁 {category}", callback_data=f"folder:{index}"
            )
        )
    markup.add(
        types.InlineKeyboardButton(tr(uid, "nav_newcat"), callback_data="planner:newcat")
    )
    markup.add(
        types.InlineKeyboardButton(tr(uid, "nav_back"), callback_data="planner:menu")
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
    text, markup = folders_view(uid)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
    except Exception:
        logger.exception("Не удалось обновить список папок")


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

def _task_due_label(uid, due_utc, timezone_name):
    """Локальная дата/время задачи «12.10 10:00» или '' если срока нет."""
    if not due_utc:
        return ""
    try:
        tz = ZoneInfo(timezone_name) if timezone_name else ZoneInfo("Europe/Moscow")
        local = datetime.fromisoformat(due_utc).astimezone(tz)
        return local.strftime("%d.%m %H:%M")
    except Exception:
        return ""


def _task_reminder_label(uid, remind_before_min):
    """Подпись напоминания «за 15 минут» или None если напоминания нет."""
    if not remind_before_min:
        return None
    labels = REMINDER_LABELS[get_user_lang(uid)]
    return labels.get(str(int(remind_before_min)), f"{int(remind_before_min)} мин")


def folder_tasks_text(uid, category):
    """Содержимое одной папки: карточки задач с напоминанием + кнопка назад."""
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT task_id, task_text, is_completed, due_utc,
                          remind_before_min, timezone
                     FROM tasks WHERE user_id = ? AND category = ?
                     ORDER BY is_completed ASC, task_id DESC""",
                (uid, category),
            ).fetchall()
        finally:
            conn.close()

    lines = [f"📁 {_esc(category)}"]
    if not rows:
        lines.append(tr(uid, "folders_empty"))
    for row in rows:
        done = bool(row["is_completed"])
        if done:
            lines.append(f"✅ <s>{_esc(row['task_text'])}</s> ✅")
        else:
            lines.append(f"🔴 {_esc(row['task_text'])}")
        remind = _task_reminder_label(uid, row["remind_before_min"])
        if remind:
            lines.append(f"   ⏰ {remind}")

    markup = types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        label = row["task_text"][:30]
        prefix = "✅" if row["is_completed"] else "📄"
        markup.add(
            types.InlineKeyboardButton(
                f"{prefix} {label}", callback_data=f"taskcard:{row['task_id']}"
            )
        )
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"
        )
    )
    return "\n".join(lines), markup


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
    text, markup = folder_tasks_text(uid, category)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text, reply_markup=markup, parse_mode="HTML"
        )


def _task_card(uid, task_id):
    """Данные задачи для карточки или None."""
    with db_lock:
        conn = get_db()
        try:
            return conn.execute(
                """SELECT task_id, task_text, category, is_completed,
                          due_utc, remind_before_min, timezone
                     FROM tasks WHERE task_id=? AND user_id=?""",
                (task_id, uid),
            ).fetchone()
        finally:
            conn.close()


def task_card_text(uid, row):
    """Текст карточки одной задачи (название + срок + напоминание)."""
    title = _esc(row["task_text"])
    if row["is_completed"]:
        lines = [f"✅ <s>{title}</s> ✅"]
    else:
        lines = [tr(uid, "task_card_title", title=title)]
    due = _task_due_label(uid, row["due_utc"], row["timezone"])
    if due:
        lines.append(tr(uid, "task_card_due", date=due))
    remind = _task_reminder_label(uid, row["remind_before_min"])
    if remind:
        lines.append(tr(uid, "task_card_reminder", value=remind))
    else:
        lines.append(tr(uid, "task_card_reminder_off"))
    return "\n".join(lines)


def task_card_markup(uid, row):
    """Кнопки карточки задачи: Сделано/Вернуть, Напомнить, Удалить, назад."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    done = bool(row["is_completed"])
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "task_undone_btn" if done else "task_done_btn"),
            callback_data=f"tasktoggle:{row['task_id']}",
        )
    )
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "task_remind_btn"),
            callback_data=f"taskrem:{row['task_id']}",
        )
    )
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "task_delete_btn"),
            callback_data=f"taskdelask:{row['task_id']}",
        )
    )
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"
        )
    )
    return markup


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskcard:"))
def show_task_card(call):
    uid = call.from_user.id
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid task")
        return
    row = _task_card(uid, task_id)
    if not row:
        bot.answer_callback_query(call.id, "Task not found")
        return
    bot.answer_callback_query(call.id)
    text = task_card_text(uid, row)
    markup = task_card_markup(uid, row)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text, reply_markup=markup, parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("tasktoggle:"))
def toggle_task_card(call):
    uid = call.from_user.id
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid task")
        return
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
    row = _task_card(uid, task_id)
    bot.answer_callback_query(call.id)
    if not row:
        return
    text = task_card_text(uid, row)
    markup = task_card_markup(uid, row)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text, reply_markup=markup, parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskdelask:"))
def ask_delete_task(call):
    """Показать подтверждение удаления задачи (безопасное удаление)."""
    uid = call.from_user.id
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid task")
        return
    row = _task_card(uid, task_id)
    if not row:
        bot.answer_callback_query(call.id, "Task not found")
        return
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "task_delete_yes"), callback_data=f"taskdelyes:{task_id}"
        ),
        types.InlineKeyboardButton(
            tr(uid, "task_delete_no"), callback_data=f"taskdelno:{task_id}"
        ),
    )
    text = (
        tr(uid, "task_delete_confirm_title")
        + "\n\n"
        + tr(uid, "task_delete_confirm_body", title=_esc(row["task_text"]))
    )
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text, reply_markup=markup, parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskdelyes:"))
def do_delete_task(call):
    """Фактическое удаление задачи из БД."""
    uid = call.from_user.id
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid task")
        return
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "DELETE FROM tasks WHERE task_id=? AND user_id=?",
                (task_id, uid),
            )
            conn.commit()
        finally:
            conn.close()
    bot.answer_callback_query(call.id, "🗑 Удалено" if get_user_lang(uid) == "ru" else "🗑 Deleted")
    # Возвращаем пользователя к списку папок.
    text, markup = folders_view(uid)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text, reply_markup=markup, parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskdelno:"))
def cancel_delete_task(call):
    """Отмена удаления — вернуть карточку задачи."""
    uid = call.from_user.id
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid task")
        return
    row = _task_card(uid, task_id)
    bot.answer_callback_query(call.id)
    if not row:
        text, markup = folders_view(uid)
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="HTML",
        )
        return
    text = task_card_text(uid, row)
    markup = task_card_markup(uid, row)
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id, text, reply_markup=markup, parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda call: call.data == "menu:main")
def back_to_main_menu(call):
    """Кнопка «⬅️ Назад в меню» — вернуться к главному меню.

    Главное меню — это постоянная клавиатура внизу, поэтому не нужно
    заново присылать длинное приветствие: просто закрываем раздел и
    показываем короткую подсказку с меню.
    """
    uid = call.from_user.id
    leave_mode(uid)
    bot.answer_callback_query(call.id, "🏠")
    try:
        bot.edit_message_text(
            tr(uid, "menu_returned"),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_keyboard(uid),
        )
    except Exception:
        bot.send_message(call.message.chat.id, tr(uid, "menu_returned"), reply_markup=main_keyboard(uid))


def task_reminder_markup(uid, task_id):
    """Кнопки выбора времени напоминания для задачи (по образцу событий)."""
    short = REMINDER_SHORT.get(get_user_lang(uid), REMINDER_SHORT["ru"])
    markup = types.InlineKeyboardMarkup(row_width=2)
    for key in REMINDER_QUICK:
        markup.add(
            types.InlineKeyboardButton(
                short[key], callback_data=f"taskremq:{task_id}:{key}"
            )
        )
    markup.row(
        types.InlineKeyboardButton(
            tr(uid, "remind_custom_btn"), callback_data=f"taskremc:{task_id}"
        )
    )
    markup.row(
        types.InlineKeyboardButton(
            tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"
        )
    )
    return markup


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskrem:"))
def task_reminder_pick(call):
    """Кнопка «⏰ Напомнить» в карточке задачи — предложить время напоминания.

    Напоминания доступны только при положительном балансе: иначе показываем
    подсказку и кнопку пополнения (оплата).
    """
    uid = call.from_user.id
    task_id = call.data.split(":", 1)[1]
    row = _task_card(uid, int(task_id)) if task_id.isdigit() else None
    if not row:
        bot.answer_callback_query(call.id, "Task not found")
        return

    paid, bonus, _tts, _chat, _img, _show = get_balance(uid)
    if (paid + bonus) <= 0:
        # Бесплатно напоминания недоступны — предлагаем оплату.
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup")
        )
        markup.add(
            types.InlineKeyboardButton(
                tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"
            )
        )
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                tr(uid, "task_remind_free_hint"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except Exception:
            bot.send_message(
                call.message.chat.id, tr(uid, "task_remind_free_hint"), reply_markup=markup
            )
        return

    if not row["due_utc"]:
        # Без даты/времени напоминание не к чему привязать.
        bot.answer_callback_query(call.id, "⚠️")
        bot.send_message(call.message.chat.id, tr(uid, "task_need_due_for_reminder"))
        return

    bot.answer_callback_query(call.id)
    markup = task_reminder_markup(uid, task_id)
    try:
        bot.edit_message_text(
            tr(uid, "remind_pick", title=row["task_text"]),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            tr(uid, "remind_pick", title=row["task_text"]),
            reply_markup=markup,
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskremq:"))
def task_reminder_quick(call):
    """Быстрая установка напоминания задачи (15/30/60 мин)."""
    uid = call.from_user.id
    try:
        _, task_id, value = call.data.split(":", 2)
        minutes = int(value)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid data")
        return

    paid, bonus, _tts, _chat, _img, _show = get_balance(uid)
    if (paid + bonus) <= 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup")
        )
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                tr(uid, "task_remind_free_hint"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except Exception:
            bot.send_message(
                call.message.chat.id, tr(uid, "task_remind_free_hint"), reply_markup=markup
            )
        return

    result = set_task_reminder(uid, int(task_id), minutes)
    if not result:
        bot.answer_callback_query(call.id, "⚠️ Укажите дату/время задачи")
        bot.send_message(call.message.chat.id, tr(uid, "task_need_due_for_reminder"))
        return
    bot.answer_callback_query(call.id, "✅")
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


@bot.callback_query_handler(func=lambda call: call.data.startswith("taskremc:"))
def task_reminder_custom(call):
    """Кнопка «Своё время» — ввод времени напоминания задачи текстом."""
    uid = call.from_user.id
    task_id = call.data.split(":", 1)[1]
    begin_input(uid, f"task_reminder:{task_id}")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, tr(uid, "remind_custom_prompt"))


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


def _offset_label(uid, minutes):
    """Подпись одного интервала: «за 15 минут» / «за 1 час» и т.п."""
    labels = REMINDER_LABELS[get_user_lang(uid)]
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return f"{minutes} мин"
    return labels.get(str(minutes), f"{minutes} мин")


def _apply_force_date(local_str, force_date):
    """Подставить выбранную в календаре дату, сохранив время (или 09:00).

    local_str может быть 'YYYY-MM-DDTHH:MM', 'HH:MM' или пустым.
    """
    import re

    time_part = "09:00"
    if local_str:
        match = re.search(r"(\d{1,2}):(\d{2})", str(local_str))
        if match:
            time_part = f"{int(match.group(1)):02d}:{match.group(2)}"
    return f"{force_date.isoformat()}T{time_part}"


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


def set_task_reminder(uid, task_id, minutes):
    """Установить одно напоминание к задаче (по due_utc). Возвращает minutes или None."""
    minutes = int(minutes)
    now = datetime.now(timezone.utc)
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT due_utc, timezone FROM tasks WHERE task_id=? AND user_id=?",
                (task_id, uid),
            ).fetchone()
            if not row or not row["due_utc"]:
                return None
            due = datetime.fromisoformat(row["due_utc"])
            if due.tzinfo is None:
                tz_name = row["timezone"] or "Europe/Moscow"
                due = due.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
            remind_at = due - timedelta(minutes=minutes)
            # Напоминание не раньше «сейчас».
            if remind_at < now:
                remind_at = now
            conn.execute(
                "UPDATE tasks SET remind_before_min=?, remind_utc=?, remind_sent=0 "
                "WHERE task_id=? AND user_id=?",
                (minutes, remind_at.isoformat(timespec="seconds"), task_id, uid),
            )
            conn.commit()
            return minutes
        finally:
            conn.close()


def clear_task_reminder(uid, task_id):
    """Убрать напоминание у задачи."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE tasks SET remind_before_min=NULL, remind_utc=NULL, remind_sent=0 "
                "WHERE task_id=? AND user_id=?",
                (task_id, uid),
            )
            conn.commit()
        finally:
            conn.close()


def _events_by_local_date(uid):
    """Словарь {локальная дата: [(название, 'HH:MM', [datetime срабатываний], kind, id, [минуты]), ...]}.

    Объединяет события (events) и задачи с датой (tasks.due_utc), чтобы на
    календаре подсвечивался любой план дня. kind: 'event' | 'task'.
    """
    user_tz_name, _off, _voice = get_user_preferences(uid)
    result = {}
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT event_id, title, start_utc, timezone, reminders_json FROM events "
                "WHERE user_id = ? AND cancelled = 0",
                (uid,),
            ).fetchall()
            task_rows = conn.execute(
                "SELECT task_id, task_text, due_utc, timezone, remind_before_min "
                "FROM tasks WHERE user_id = ? AND due_utc IS NOT NULL",
                (uid,),
            ).fetchall()
        finally:
            conn.close()

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
            (row["title"], local.strftime("%H:%M"), fire_times, "event", row["event_id"], offsets)
        )

    for row in task_rows:
        try:
            due = datetime.fromisoformat(row["due_utc"])
            if due.tzinfo is None:
                tz = ZoneInfo(row["timezone"] or user_tz_name)
                due = due.replace(tzinfo=tz).astimezone(timezone.utc)
            local = due.astimezone(ZoneInfo(row["timezone"] or user_tz_name))
        except (ValueError, ZoneInfoNotFoundError):
            continue
        fire_times = []
        task_offsets = []
        if row["remind_before_min"]:
            task_offsets = [int(row["remind_before_min"])]
            fire_times.append(local - timedelta(minutes=int(row["remind_before_min"])))
        result.setdefault(local.date(), []).append(
            (row["task_text"], local.strftime("%H:%M"), fire_times, "task", row["task_id"], task_offsets)
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
    """Календарь месяца: кликабельная сетка дней, подсветка дней с планами.

    Дни с событиями/задачами отмечаются • и выделены, любой день — кнопка
    (можно наговорить план на выбранную дату). Плюс навигация по месяцам/годам,
    «Сегодня» и статус пробного периода календаря.
    """
    lang = get_user_lang(uid)
    events = _events_by_local_date(uid)
    month_name = MONTHS[lang][month - 1]
    weeks = calmod.Calendar(firstweekday=0).monthdayscalendar(year, month)
    today_local = _now_in_user_tz(uid).date()

    lines = [tr(uid, "cal_title", month=f"{month_name} {year}"), ""]
    lines.append(WEEKDAYS_HEADER[lang])
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("   ")
            else:
                mark = "•" if date(year, month, day) in events else " "
                cells.append(f"{day:>2}{mark}")
        lines.append(" ".join(cells).rstrip())

    # Легенда: дни с планами в этом месяце.
    days_with = sorted(d for d in events if d.year == year and d.month == month)
    lines.append("")
    if days_with:
        lines.append(
            tr(uid, "cal_legend") + " " + ", ".join(str(d.day) for d in days_with)
        )
    else:
        lines.append(tr(uid, "cal_no_dates"))

    # Подсказка: как добавить план на выбранный день.
    lines.append("")
    lines.append(tr(uid, "cal_tap_day_hint"))

    # Статус доступа к календарю (пробный период / активен / нужна оплата).
    access_ok, access_reason = calendar_access(uid)
    if access_ok and access_reason == "trial":
        lines.append("")
        lines.append(tr(uid, "cal_legend_trial", days=calendar_trial_days_left(uid)))

    markup = types.InlineKeyboardMarkup(row_width=7)
    # Навигация: ◀︎ год, ◀︎ месяц, Сегодня, месяц ▶︎, год ▶︎.
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    markup.row(
        types.InlineKeyboardButton("⇤", callback_data=f"calnav:{year - 1:04d}-{month:02d}"),
        types.InlineKeyboardButton("⬅️", callback_data=f"calnav:{prev_y:04d}-{prev_m:02d}"),
        types.InlineKeyboardButton(tr(uid, "cal_today"), callback_data="caltoday"),
        types.InlineKeyboardButton("➡️", callback_data=f"calnav:{next_y:04d}-{next_m:02d}"),
        types.InlineKeyboardButton("⇥", callback_data=f"calnav:{year + 1:04d}-{month:02d}"),
    )
    # Сетка дней: каждый день — кнопка, дни с планами подсвечены «•».
    for week in weeks:
        row_buttons = []
        for day in week:
            if day == 0:
                row_buttons.append(
                    types.InlineKeyboardButton(" ", callback_data="calnoop")
                )
            else:
                has = date(year, month, day) in events
                label = f"•{day}•" if has else str(day)
                row_buttons.append(
                    types.InlineKeyboardButton(
                        label, callback_data=f"calday:{date(year, month, day).isoformat()}"
                    )
                )
        markup.row(*row_buttons)

    # Возврат в меню раздела (единообразно с папками).
    markup.row(
        types.InlineKeyboardButton(tr(uid, "nav_back"), callback_data="planner:menu"),
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
    uid = message.from_user.id
    access_ok, _reason = calendar_access(uid)
    if not access_ok:
        _calendar_locked(message.chat.id, uid)
        return
    now = _now_in_user_tz(uid)
    _send_or_edit_calendar(message, uid, now.year, now.month)


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


def _build_day_card(uid, day):
    """Текст и кнопки карточки дня: планы дня + напоминания + удаление."""
    access_ok, _reason = calendar_access(uid)
    items = _events_by_local_date(uid).get(day, [])
    date_iso = day.isoformat()

    lines = [tr(uid, "cal_day_title", date=day.strftime("%d.%m.%Y"))]
    markup = types.InlineKeyboardMarkup(row_width=1)
    has_add_button = False
    for title, time_hhmm, fire_times, kind, item_id, offsets in items:
        lines.append(f"• {title} — {time_hhmm}")
        if offsets:
            # Несколько напоминаний — каждое на своей строке с будильником.
            for minutes in sorted({int(x) for x in offsets}):
                lines.append(f"   ⏰ {_offset_label(uid, minutes)}")
        elif access_ok:
            # Напоминания нет — своя кнопка с названием плана, чтобы не путаться.
            has_add_button = True
            markup.add(
                types.InlineKeyboardButton(
                    f"⏰ {title[:24]}",
                    callback_data=f"calrem:{date_iso}:{kind}:{item_id}",
                )
            )
        if access_ok and offsets:
            # Напоминание есть — кнопка изменения (в пикере есть «убрать»).
            markup.add(
                types.InlineKeyboardButton(
                    f"{tr(uid, 'cal_remind_change_btn')} {title[:16]}",
                    callback_data=f"calrem:{date_iso}:{kind}:{item_id}",
                )
            )
        # У каждого плана — кнопка удаления (подтверждение на следующем шаге).
        markup.add(
            types.InlineKeyboardButton(
                f"{tr(uid, 'cal_delete_btn')} {title[:20]}",
                callback_data=f"caldelask:{date_iso}:{kind}:{item_id}",
            )
        )

    if not items:
        lines.append(tr(uid, "cal_day_empty"))
    elif has_add_button:
        lines.append(tr(uid, "cal_reminder_below"))

    # У каждой даты одинаковая навигация: вернуться к календарю + компактный доступ.
    markup.row(
        types.InlineKeyboardButton(tr(uid, "cal_back"), callback_data="planner:menu_calendar"),
        types.InlineKeyboardButton(tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"),
    )
    return "\n".join(lines), markup


@bot.callback_query_handler(func=lambda call: call.data.startswith("calday:"))
def calendar_open_day(call):
    uid = call.from_user.id
    try:
        day = date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return

    # Запоминаем выбранную дату: следующее наговоренное/написанное
    # сообщение попадёт на этот день.
    selected_cal_date[uid] = day

    bot.answer_callback_query(call.id)
    text, markup = _build_day_card(uid, day)
    bot.send_message(call.message.chat.id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "calnoop")
def calendar_noop(call):
    bot.answer_callback_query(call.id)


def _plan_title(uid, kind, item_id):
    """Название события/задачи для подтверждения удаления или None."""
    if kind == "event":
        table, id_col, title_col = "events", "event_id", "title"
    elif kind == "task":
        table, id_col, title_col = "tasks", "task_id", "task_text"
    else:
        return None
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                f"SELECT {title_col} FROM {table} WHERE {id_col}=? AND user_id=?",
                (item_id, uid),
            ).fetchone()
        finally:
            conn.close()
    return row[title_col] if row else None


def _delete_plan(uid, kind, item_id):
    """Удалить событие (мягко, cancelled=1) или задачу (полностью)."""
    with db_lock:
        conn = get_db()
        try:
            if kind == "event":
                conn.execute(
                    "UPDATE events SET cancelled=1 WHERE event_id=? AND user_id=?",
                    (item_id, uid),
                )
                # Гасим напоминания удалённого события.
                conn.execute(
                    "UPDATE reminders SET sent=1 WHERE event_id=? AND user_id=?",
                    (item_id, uid),
                )
            elif kind == "task":
                conn.execute(
                    "DELETE FROM tasks WHERE task_id=? AND user_id=?",
                    (item_id, uid),
                )
            else:
                return False
            conn.commit()
        finally:
            conn.close()
    return True


def _edit_or_send(call, text, markup):
    """Отредактировать сообщение карточки дня или отправить новое."""
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("caldelask:"))
def calendar_ask_delete(call):
    """Кнопка «🗑 Удалить» в карточке дня: подтверждение."""
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) != 4:
        bot.answer_callback_query(call.id, "Invalid data")
        return
    _, date_iso, kind, item_id = parts
    try:
        day = date.fromisoformat(date_iso)
        item_id = int(item_id)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid data")
        return
    title = _plan_title(uid, kind, item_id)
    if not title:
        bot.answer_callback_query(call.id, "Plan not found")
        return

    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "task_delete_yes"),
            callback_data=f"caldelyes:{date_iso}:{kind}:{item_id}",
        ),
        types.InlineKeyboardButton(
            tr(uid, "task_delete_no"),
            callback_data=f"caldelno:{date_iso}",
        ),
    )
    text = (
        tr(uid, "cal_delete_confirm_title")
        + "\n\n"
        + tr(uid, "cal_delete_confirm_body", title=title)
    )
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("caldelyes:"))
def calendar_do_delete(call):
    """Подтверждение «Да, удалить»: удаляем план и показываем обновлённый день."""
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) != 4:
        bot.answer_callback_query(call.id, "Invalid data")
        return
    _, date_iso, kind, item_id = parts
    try:
        day = date.fromisoformat(date_iso)
        item_id = int(item_id)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid data")
        return

    if not _delete_plan(uid, kind, item_id):
        bot.answer_callback_query(call.id, "Plan not found")
        return

    bot.answer_callback_query(call.id, "🗑 Удалено" if get_user_lang(uid) == "ru" else "🗑 Deleted")
    selected_cal_date[uid] = day
    text, markup = _build_day_card(uid, day)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("caldelno:"))
def calendar_cancel_delete(call):
    """Отмена удаления — вернуть карточку дня."""
    uid = call.from_user.id
    try:
        day = date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return
    bot.answer_callback_query(call.id)
    text, markup = _build_day_card(uid, day)
    _edit_or_send(call, text, markup)


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
    text, markup = folders_view(uid)
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


def _edit_events_list(call):
    """Перерисовать список ближайших событий тем же сообщением."""
    text, markup = upcoming_events_text(call.from_user.id)
    _edit_or_send(call, text, markup)


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
    bot.answer_callback_query(call.id, "✅")
    # Сразу показываем обновлённый список — напоминание видно сразу.
    _edit_events_list(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtpick:"))
def event_reminder_pick(call):
    """Кнопка ⏰/🔄 в списке событий — пикер времени на месте (редактирует сообщение)."""
    uid = call.from_user.id
    try:
        event_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid event")
        return
    access_ok, _reason = calendar_access(uid)
    if not access_ok:
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
        bot.send_message(call.message.chat.id, tr(uid, "cal_reminder_free_hint"), reply_markup=markup)
        return

    # Текущее напоминание события (если есть) — показываем в пикере.
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT reminders_json FROM events WHERE event_id=? AND user_id=?",
                (event_id, uid),
            ).fetchone()
        finally:
            conn.close()
    offsets = []
    if row:
        try:
            offsets = sorted({int(x) for x in json.loads(row["reminders_json"] or "[]")})
        except (ValueError, TypeError, json.JSONDecodeError):
            offsets = []

    short = REMINDER_SHORT.get(get_user_lang(uid), REMINDER_SHORT["ru"])
    lines = [tr(uid, "cal_day_pick_format")]
    if offsets:
        current = ", ".join(_offset_label(uid, m) for m in offsets)
        lines.append(tr(uid, "cal_reminder_current", value=current))
    markup = types.InlineKeyboardMarkup(row_width=2)
    for key in REMINDER_QUICK:
        markup.add(
            types.InlineKeyboardButton(
                short[key], callback_data=f"evtrem:{event_id}:{key}"
            )
        )
    markup.row(
        types.InlineKeyboardButton(
            tr(uid, "remind_custom_btn"), callback_data=f"evtremc:{event_id}"
        )
    )
    if offsets:
        markup.row(
            types.InlineKeyboardButton(
                tr(uid, "cal_remind_off_btn"), callback_data=f"evtoff:{event_id}"
            )
        )
    markup.row(
        types.InlineKeyboardButton(tr(uid, "evt_back"), callback_data="evtback")
    )
    bot.answer_callback_query(call.id)
    _edit_or_send(call, "\n".join(lines), markup)


@bot.callback_query_handler(func=lambda call: call.data == "evtback")
def event_reminder_back(call):
    """Кнопка «назад» из пикера списка событий."""
    bot.answer_callback_query(call.id)
    _edit_events_list(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtoff:"))
def event_reminder_off(call):
    """Кнопка «Убрать напоминание» в списке событий."""
    uid = call.from_user.id
    try:
        event_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid event")
        return
    if not _clear_plan_reminder(uid, "event", event_id):
        bot.answer_callback_query(call.id, "Event not found")
        return
    bot.answer_callback_query(call.id, "🚫")
    _edit_events_list(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtremc:"))
def event_reminder_custom(call):
    """Кнопка «Своё время» — просим ввести время напоминания текстом."""
    uid = call.from_user.id
    event_id = call.data.split(":", 1)[1]
    begin_input(uid, f"custom_reminder:{event_id}")
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(tr(uid, "evt_back"), callback_data="evtback"))
    bot.send_message(call.message.chat.id, tr(uid, "remind_custom_prompt"), reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("evtrems:"))
def event_reminder_skip(call):
    """Кнопка «Без напоминания» (легаси) — вернуть актуальный список."""
    bot.answer_callback_query(call.id)
    _edit_events_list(call)


def _set_plan_reminder(uid, kind, item_id, minutes):
    """Установить напоминание событию/задаче. True при успехе."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    if kind == "event":
        return bool(set_event_reminder(uid, item_id, minutes))
    if kind == "task":
        return bool(set_task_reminder(uid, item_id, minutes))
    return False


def _clear_plan_reminder(uid, kind, item_id):
    """Убрать напоминание у события/задачи. True при успехе."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    if kind == "event":
        with db_lock:
            conn = get_db()
            try:
                changed = conn.execute(
                    "UPDATE events SET reminders_json='[]' WHERE event_id=? AND user_id=?",
                    (item_id, uid),
                ).rowcount
                # Гасим уже поставленные строки напоминаний.
                conn.execute(
                    "UPDATE reminders SET sent=1 WHERE event_id=? AND user_id=?",
                    (item_id, uid),
                )
                conn.commit()
            finally:
                conn.close()
        return bool(changed)
    if kind == "task":
        return bool(clear_task_reminder(uid, item_id))
    return False


def _send_calendar_reminder_picker(call, date_iso, kind, item_id):
    """Пикер времени напоминания — редактирует сообщение карточки дня на месте."""
    uid = call.from_user.id
    short = REMINDER_SHORT.get(get_user_lang(uid), REMINDER_SHORT["ru"])
    # Текущее напоминание (если есть) — показываем в заголовке пикера.
    offsets = []
    try:
        items = _events_by_local_date(uid).get(date.fromisoformat(date_iso), [])
    except ValueError:
        items = []
    for item in items:
        if item[3] == kind and str(item[4]) == str(item_id):
            offsets = item[5]
            break
    lines = [tr(uid, "cal_day_pick_format")]
    if offsets:
        current = ", ".join(_offset_label(uid, m) for m in sorted({int(x) for x in offsets}))
        lines.append(tr(uid, "cal_reminder_current", value=current))
    markup = types.InlineKeyboardMarkup(row_width=2)
    for key in REMINDER_QUICK:
        markup.add(
            types.InlineKeyboardButton(
                short[key], callback_data=f"calremq:{date_iso}:{kind}:{item_id}:{key}"
            )
        )
    markup.row(
        types.InlineKeyboardButton(
            tr(uid, "remind_custom_btn"), callback_data=f"calremc:{date_iso}:{kind}:{item_id}"
        )
    )
    if offsets:
        markup.row(
            types.InlineKeyboardButton(
                tr(uid, "cal_remind_off_btn"),
                callback_data=f"calrmoff:{date_iso}:{kind}:{item_id}",
            )
        )
    markup.row(
        types.InlineKeyboardButton(
            tr(uid, "cal_back"), callback_data=f"calrmback:{date_iso}"
        )
    )
    _edit_or_send(call, "\n".join(lines), markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calrmback:"))
def calendar_reminder_back(call):
    """Кнопка «назад» из пикера — вернуть карточку дня тем же сообщением."""
    uid = call.from_user.id
    try:
        day = date.fromisoformat(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return
    bot.answer_callback_query(call.id)
    text, markup = _build_day_card(uid, day)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calrmoff:"))
def calendar_reminder_off(call):
    """Кнопка «Убрать напоминание» в пикере календаря."""
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) != 4:
        bot.answer_callback_query(call.id, "Invalid data")
        return
    _, date_iso, kind, item_id = parts
    try:
        day = date.fromisoformat(date_iso)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return
    if not _clear_plan_reminder(uid, kind, item_id):
        bot.answer_callback_query(call.id, "Plan not found")
        return
    bot.answer_callback_query(call.id, "🚫")
    text, markup = _build_day_card(uid, day)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calrem:"))
def calendar_add_reminder(call):
    """Кнопка «⏰ Поставить/Изменить напоминание» в карточке дня календаря."""
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) != 4 or parts[2] not in ("event", "task"):
        bot.answer_callback_query(call.id, "Invalid data")
        return
    _, date_iso, kind, item_id = parts
    try:
        date.fromisoformat(date_iso)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return
    access_ok, _reason = calendar_access(uid)
    if not access_ok:
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
        bot.send_message(call.message.chat.id, tr(uid, "cal_reminder_free_hint"), reply_markup=markup)
        return
    bot.answer_callback_query(call.id)
    _send_calendar_reminder_picker(call, date_iso, kind, item_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calremq:"))
def calendar_reminder_quick(call):
    """Быстрый выбор времени напоминания из календаря (редактирует сообщение на месте)."""
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) != 5:
        bot.answer_callback_query(call.id, "Invalid data")
        return
    _, date_iso, kind, item_id, value = parts
    try:
        day = date.fromisoformat(date_iso)
        minutes = int(value)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid interval")
        return
    access_ok, _reason = calendar_access(uid)
    if not access_ok:
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
        bot.send_message(call.message.chat.id, tr(uid, "cal_reminder_free_hint"), reply_markup=markup)
        return
    if not _set_plan_reminder(uid, kind, item_id, minutes):
        bot.answer_callback_query(call.id, "Plan not found")
        return
    bot.answer_callback_query(call.id, "✅")
    # Сразу показываем обновлённую карточку дня — действие видно сразу.
    text, markup = _build_day_card(uid, day)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("calremc:"))
def calendar_reminder_custom(call):
    """Своё время напоминания (голосом/текстом) из карточки дня календаря."""
    uid = call.from_user.id
    parts = call.data.split(":")
    if len(parts) != 4 or parts[2] not in ("event", "task"):
        bot.answer_callback_query(call.id, "Invalid data")
        return
    _, date_iso, kind, item_id = parts
    try:
        date.fromisoformat(date_iso)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid date")
        return
    begin_input(uid, f"calrem:{date_iso}:{kind}:{item_id}")
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            tr(uid, "cal_back"), callback_data=f"calrmback:{date_iso}"
        )
    )
    bot.send_message(
        call.message.chat.id, tr(uid, "cal_day_pick_format"), reply_markup=markup
    )


# ============================================================
# 6-bis. РАЗДЕЛ «ЗАДАЧИ И КАЛЕНДАРЬ»: CALLBACK-НАВИГАЦИЯ
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("planner:"))
def planner_router(call):
    """Единый раздел: меню, мои задачи, папки, календарь, события, новая папка."""
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

    if action in {"menu", "tasks"}:
        bot.answer_callback_query(call.id)
        text, markup = planner_menu(uid)
        edit_or_send(text, markup)
    elif action == "menu_tasks":
        bot.answer_callback_query(call.id)
        text, markup = folders_view(uid)
        edit_or_send(text, markup)
    elif action == "goto_folders":
        bot.answer_callback_query(call.id)
        text, markup = folders_view(uid)
        edit_or_send(text, markup)
    elif action in {"menu_calendar", "calendar"}:
        access_ok, _reason = calendar_access(uid)
        if not access_ok:
            bot.answer_callback_query(call.id)
            _calendar_locked(call.message.chat.id, uid)
            return
        bot.answer_callback_query(call.id)
        now = _now_in_user_tz(uid)
        _send_or_edit_calendar(call, uid, now.year, now.month)
    elif action == "folders":
        bot.answer_callback_query(call.id)
        text, markup = folders_view(uid)
        edit_or_send(text, markup)
    elif action == "newcat":
        bot.answer_callback_query(call.id)
        begin_input(uid, "category")
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
            timeout=(30, 120),
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
- Urgent: explicitly urgent tasks due now/today.
- Daily: routine everyday tasks (shopping, calls, errands).
- Important: important but not urgent tasks.
Do not put every item in one category. If the user mentions a date or time
for a task (e.g. "call tomorrow at 10:00"), keep it as a task in the most
fitting category AND also create a calendar event so it appears on the date.
"""
        response_language = "English"
    else:
        rules = """
Распределяй каждую новую задачу ровно в одну из предоставленных категорий:
- 🔴 Срочно — только явно срочные дела, которые нужно сделать сейчас или сегодня.
- 📋 Повседневное — обычные повседневные задачи (покупки, звонки, поручения).
- ⭐ Важно — важные, но не срочные задачи.
Если пользователь упомянул дату или время задачи (например, «позвонить завтра в 10:00»),
оставь это задачей в подходящей категории и дополнительно создай событие в календаре,
чтобы оно отобразилось на нужной дате.
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
   If the user gives a date/time for a task, also set "due_datetime" to the local
   ISO datetime YYYY-MM-DDTHH:MM (else null). If the user asks to be reminded
   BEFORE the task (e.g. "remind me 30 minutes before"), set "remind_minutes"
   to that number of minutes (else null).
3. new_events: events with title, local_datetime in ISO format YYYY-MM-DDTHH:MM,
   or null if the date/time is unclear.
   The title must NOT contain the date or the time — they are shown separately.
4. For each event, if the user explicitly asks to be reminded BEFORE it
   (e.g. "remind me 30 minutes before", "remind me 2 hours before",
   "remind me a day before"), set remind_minutes to that number of minutes
   (30, 120, 1440, ...). If no reminder time is stated, set remind_minutes to null.

If date is relative (e.g. tomorrow), resolve it using the current date:
{datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")}.

JSON schema:
{{
  "completed_task_ids": [],
  "new_tasks": [{{"category": "exact category", "text": "task", "due_datetime": null, "remind_minutes": null}}],
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
        timeout=(30, 60),
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


def _local_to_utc_iso(local_datetime, timezone_name):
    """Локальная дата/время (YYYY-MM-DDTHH:MM) → ISO в UTC. None при ошибке."""
    try:
        naive = datetime.fromisoformat(local_datetime)
        if naive.tzinfo is None:
            naive = naive.replace(tzinfo=ZoneInfo(timezone_name))
        return naive.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, ZoneInfoNotFoundError):
        return None


def save_ai_result(uid, data, use_voice_limit=False, force_date=None):
    free, premium = get_user_data(uid)
    _paid, _bonus, _tts, _chat, _image, _show = get_balance(uid)
    has_paid_balance = (_paid + _bonus) > 0
    inserted_tasks = 0
    inserted_events = 0
    completed = 0
    no_reminder_events = []  # [(event_id, title)] — созданы без напоминания
    no_reminder_tasks = []   # [(text, want_minutes)] — напоминание недоступно без баланса

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
                due_utc = None
                remind_before = None
                remind_utc = None
                due_raw = item.get("due_datetime")
                if force_date and not due_raw:
                    due_raw = _apply_force_date(None, force_date)
                if due_raw:
                    due_utc = _local_to_utc_iso(str(due_raw), timezone_name)
                # Напоминание к задаче — только при положительном балансе.
                if due_utc:
                    try:
                        want = int(item.get("remind_minutes"))
                    except (TypeError, ValueError):
                        want = None
                    if want and 1 <= want <= 7 * 1440:
                        if has_paid_balance:
                            remind_before = want
                            due_dt = datetime.fromisoformat(due_utc)
                            fire = due_dt - timedelta(minutes=want)
                            if fire < datetime.now(timezone.utc):
                                fire = datetime.now(timezone.utc)
                            remind_utc = fire.isoformat(timespec="seconds")
                        else:
                            no_reminder_tasks.append((text, want))
                conn.execute(
                    """INSERT INTO tasks
                       (user_id, category, task_text, due_utc, remind_before_min,
                        remind_utc, remind_sent, timezone)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                    (uid, category, text[:500], due_utc, remind_before,
                     remind_utc, timezone_name),
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
        if force_date:
            local_datetime = _apply_force_date(local_datetime, force_date)
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

    return (
        inserted_tasks,
        inserted_events,
        completed,
        no_reminder_events,
        no_reminder_tasks,
    )


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
    # Бесплатная пробная озвучка имеет приоритет над балансом;
    # в guard-режиме пробы приостановлены — только баланс.
    use_trial = tts_free > 0 and not guard_on()
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

def send_task_reminder_upsell(chat_id, uid, no_reminder_tasks):
    """Сообщить, что напоминания к задачам доступны при положительном балансе."""
    if not no_reminder_tasks:
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
    lines = [tr(uid, "task_reminder_need_balance")]
    for text, want in no_reminder_tasks[:5]:
        lines.append(tr(uid, "task_reminder_need_balance_item", task=text))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    uid = message.from_user.id
    # Антифлуд — раньше любых действий: Whisper стоит денег, флуд не должен
    # ни списывать квоты, ни занимать транскрибацию.
    if not flood_gate(message):
        return
    if not oferta_gate(message):
        return
    log_activity(uid)

    # Своё время напоминания голосом (из карточки дня календаря).
    _vstate = user_states.get(uid)
    if isinstance(_vstate, str) and _vstate.startswith("calrem:"):
        status = bot.reply_to(message, tr(uid, "processing"))
        path = os.path.join(tempfile.gettempdir(), f"voiceplan_{uid}_{message.message_id}.ogg")
        try:
            file_info = bot.get_file(message.voice.file_id)
            audio = bot.download_file(file_info.file_path)
            with open(path, "wb") as output:
                output.write(audio)
            transcript = transcribe_voice(path)
            minutes = parse_custom_reminder(transcript)
            if minutes is None:
                bot.reply_to(message, tr(uid, "remind_custom_bad"))
                return
            access_ok, _reason = calendar_access(uid)
            if not access_ok:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
                user_states.pop(uid, None)
                bot.reply_to(message, tr(uid, "cal_reminder_free_hint"), reply_markup=markup)
                return
            _, _date_iso, kind, item_id = _vstate.split(":", 3)
            user_states.pop(uid, None)
            if not _set_plan_reminder(uid, kind, item_id, minutes):
                bot.reply_to(message, tr(uid, "api_error"))
                return
            bot.reply_to(message, tr(uid, "cal_reminder_added", value=_offset_label(uid, minutes)))
        except Exception:
            logger.exception("Voice calendar reminder failed for user %s", uid)
            bot.send_message(message.chat.id, tr(uid, "api_error"))
        finally:
            if os.path.exists(path):
                os.remove(path)
        return

    # В режиме «Чат с ИИ» голос — это вопрос нейросети, а не задача.
    # Распознаём речь и отдаём её в общий обработчик чата (ответ текстом).
    if ui_mode(uid) == "chat":
        status = bot.reply_to(message, tr(uid, "processing"))
        path = os.path.join(
            tempfile.gettempdir(), f"voiceplan_{uid}_{message.message_id}.ogg"
        )
        try:
            file_info = bot.get_file(message.voice.file_id)
            audio = bot.download_file(file_info.file_path)
            with open(path, "wb") as output:
                output.write(audio)

            transcript = transcribe_voice(path)
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
            if not transcript:
                bot.reply_to(message, tr(uid, "no_speech"))
                return
            process_chat_message(message.chat.id, uid, transcript, reply_to=message.message_id)
        except Exception:
            logger.exception("Voice chat failed for user %s", uid)
            try:
                bot.edit_message_text(tr(uid, "api_error"), message.chat.id, status.message_id)
            except Exception:
                bot.send_message(message.chat.id, tr(uid, "api_error"))
        finally:
            if os.path.exists(path):
                os.remove(path)
        return

    allowed, reason = try_consume_voice_task(uid)
    if not allowed:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
        bot.send_message(
            uid,
            tr(uid, "voice_tasks_limit", price=f"{VOICE_TASK_PRICE_RUB:g}"),
            reply_markup=markup,
        )
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
        force_date = selected_cal_date.pop(uid, None)
        counts = save_ai_result(uid, result, use_voice_limit=False, force_date=force_date)

        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass

        follow = types.InlineKeyboardMarkup()
        follow.add(types.InlineKeyboardButton(
            tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"
        ))

        # Одно сообщение: подтверждение + актуальные события с кнопками
        # напоминания/удаления. Раньше здесь был поток из 3+ сообщений.
        if counts[1]:
            events_text, events_markup = upcoming_events_text(uid)
            if events_markup is not None:
                bot.send_message(
                    message.chat.id,
                    tr(uid, "task_added") + "\n\n" + events_text,
                    reply_markup=events_markup,
                )
            else:
                bot.send_message(message.chat.id, tr(uid, "task_added"), reply_markup=follow)
        else:
            bot.send_message(message.chat.id, tr(uid, "task_added"), reply_markup=follow)

        # Напоминания к задачам недоступны без положительного баланса.
        send_task_reminder_upsell(message.chat.id, uid, counts[4])

    except Exception:
        logger.exception("Voice handling failed for user %s", uid)
        try:
            bot.edit_message_text(tr(uid, "api_error"), message.chat.id, status.message_id)
        except Exception:
            bot.send_message(message.chat.id, tr(uid, "api_error"))
    finally:
        if os.path.exists(path):
            os.remove(path)


def chat_answer_markup(uid, answer):
    """Клавиатура под ответом ИИ: «🔊 Озвучить» (если озвучка доступна) + выход."""
    markup = types.InlineKeyboardMarkup()
    paid, bonus, tts_free, _chat, _img, _show = get_balance(uid)
    tts_cost = round(len(answer[:3500]) / 1000 * PRICE_TTS_RUB_PER_1K, 3)
    # Кнопка озвучки появляется только если хватает пробных попыток или баланса
    # (в guard-режиме пробы приостановлены — кнопка только при балансе).
    can_speak = bool(TTS_API_URL and TTS_API_KEY) and (
        (tts_free > 0 and not guard_on()) or (paid + bonus) >= tts_cost
    )
    if can_speak:
        markup.add(types.InlineKeyboardButton(tr(uid, "btn_speak"), callback_data="chat:speak"))
    markup.add(types.InlineKeyboardButton(tr(uid, "btn_exit_chat"), callback_data="chat:exit"))
    return markup


def process_chat_message(chat_id, uid, text, reply_to=None):
    """Единая обработка вопроса в чате с ИИ (из текста или голоса).

    Списывает квоту/баланс, получает ответ нейросети и присылает его ТЕКСТОМ.
    Если у пользователя есть пробные озвучки или баланс — под текстом
    появляется кнопка «🔊 Озвучить».
    """
    free, premium = get_user_data(uid)
    paid, bonus, tts_free, chat_left, _img, _show = get_balance(uid)

    # Порядок списания: дневная бесплатная норма → баланс → отказ.
    # Легаси-Premium (старая подписка) ходит без списания.
    # Чат пофактовый: до запроса проверяем лишь минималку (CHAT_MIN_RUB),
    # точная цена считается по токенам ответа (chat_price_rub).
    #
    # Лимиты токенов — только для бесплатки (и лаконичного режима); платящие
    # ограничиваются лишь потолком цены CHAT_MAX_RUB — они платят за токены.
    available = round(float(paid) + float(bonus), 2)
    # Режим защиты (/guard): дневные бесплатные квоты приостановлены для всех,
    # кроме легаси-Premium — работают только предоплаченные балансы.
    if guard_on() and not premium:
        chat_left = 0
    if not premium and chat_left > 0 and len(text) > FREE_MSG_MAX_CHARS:
        # Бесплатный запрос длиннее лимита — честно просим сократить.
        bot.send_message(
            chat_id,
            tr(uid, "msg_too_long", chars=len(text), max=FREE_MSG_MAX_CHARS),
            reply_to_message_id=reply_to,
            reply_markup=balance_open_markup(uid),
        )
        return

    spend_amount = 0.0
    daily_free = True
    short_mode = False
    if premium:
        answer_tokens = PAID_ANSWER_MAX_TOKENS
    elif chat_left > 0:
        answer_tokens = FREE_ANSWER_MAX_TOKENS
    elif available >= CHAT_MIN_RUB:
        daily_free = False
        # Лаконичный режим: при крошечном балансе отвечаем коротко, чтобы
        # фактическая цена запроса не превысила остаток (без клампа).
        short_mode = available < CHAT_SHORT_MODE_RUB
        answer_tokens = SHORT_ANSWER_MAX_TOKENS if short_mode else PAID_ANSWER_MAX_TOKENS
    else:
        bot.send_message(
            chat_id, tr(uid, free_blocked_key(uid)), reply_to_message_id=reply_to,
            reply_markup=balance_open_markup(uid),
        )
        return

    status = bot.send_message(chat_id, tr(uid, "processing"), reply_to_message_id=reply_to)
    try:
        answer, usage = ai_chat_reply(uid, text, max_tokens=answer_tokens)
        if daily_free and not premium:
            consume_daily(uid, "chat")
        elif not daily_free:
            price = chat_price_rub(
                usage.get("prompt_tokens"), usage.get("completion_tokens")
            )
            # Ответ уже сгенерирован — списываем фактическую цену, но не
            # больше доступного остатка (не уходим в минус, вариант «кламп»).
            paid_now, bonus_now, *_ = get_balance(uid)
            available = round(float(paid_now) + float(bonus_now), 2)
            spend_amount = min(price, available)
            if spend_amount > 0:
                spend_from_balance(uid, spend_amount)
            maybe_receipt(uid, spend_amount, "spend_chat")

        # Запоминаем ответ, чтобы кнопка «Озвучить» знала, что озвучивать.
        last_answer[uid] = answer
        markup = chat_answer_markup(uid, answer)

        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass

        # Сообщение Telegram не длиннее 4096 символов — режем на части.
        for start in range(0, len(answer), 4000):
            chunk = answer[start:start + 4000]
            bot.send_message(
                chat_id,
                chunk,
                reply_to_message_id=reply_to if start == 0 else None,
                reply_markup=markup if start + 4000 >= len(answer) else None,
            )

        # Лаконичный режим: подсказываем, почему ответ короткий и что делать,
        # если нужен развёрнутый (пополнить баланс).
        if short_mode:
            bot.send_message(
                chat_id,
                tr(
                    uid,
                    "short_mode_notice",
                    balance=f"{available:.2f}",
                    chars=int(SHORT_ANSWER_MAX_TOKENS * 2.5),
                ),
            )
    except Exception:
        logger.exception("AI chat failed for user %s", uid)
        try:
            bot.edit_message_text(tr(uid, "api_error"), chat_id, status.message_id)
        except Exception:
            bot.send_message(chat_id, tr(uid, "api_error"), reply_to_message_id=reply_to)


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
        # Подтверждение + кнопка «к списку»: по тапу сообщение станет
        # актуальным списком событий с уже установленным напоминанием.
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(uid, "evt_back"), callback_data="evtback"))
        bot.reply_to(
            message,
            tr(uid, "remind_done", value=format_reminder_labels(uid, [minutes])),
            reply_markup=markup,
        )
        return

    # Ввод своего времени напоминания для конкретной задачи.
    if isinstance(state, str) and state.startswith("task_reminder:"):
        task_id = state.split(":", 1)[1]
        minutes = parse_custom_reminder(text)
        if minutes is None:
            bot.reply_to(message, tr(uid, "remind_custom_bad"))
            return
        try:
            task_id_int = int(task_id)
        except (TypeError, ValueError):
            user_states.pop(uid, None)
            bot.reply_to(message, tr(uid, "api_error"))
            return
        paid, bonus, _tts, _chat, _img, _show = get_balance(uid)
        if (paid + bonus) <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup")
            )
            user_states.pop(uid, None)
            bot.reply_to(message, tr(uid, "task_remind_free_hint"), reply_markup=markup)
            return
        result = set_task_reminder(uid, task_id_int, minutes)
        user_states.pop(uid, None)
        if not result:
            bot.reply_to(message, tr(uid, "task_need_due_for_reminder"))
            return
        bot.reply_to(
            message, tr(uid, "remind_done", value=format_reminder_labels(uid, [minutes]))
        )
        return

    # Своё время напоминания для события/задачи из календаря.
    if isinstance(state, str) and state.startswith("calrem:"):
        _, _date_iso, kind, item_id = state.split(":", 3)
        minutes = parse_custom_reminder(text)
        if minutes is None:
            bot.reply_to(message, tr(uid, "remind_custom_bad"))
            return
        access_ok, _reason = calendar_access(uid)
        if not access_ok:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(tr(uid, "bal_topup"), callback_data="bal:topup"))
            user_states.pop(uid, None)
            bot.reply_to(message, tr(uid, "cal_reminder_free_hint"), reply_markup=markup)
            return
        user_states.pop(uid, None)
        if not _set_plan_reminder(uid, kind, item_id, minutes):
            bot.reply_to(message, tr(uid, "api_error"))
            return
        bot.reply_to(message, tr(uid, "cal_reminder_added", value=_offset_label(uid, minutes)))
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
    if ui_mode(uid) == "chat":
        process_chat_message(message.chat.id, uid, text, reply_to=message.message_id)
        return

    # Режим «🎨 Картинки»: текст — промпт для генерации изображения.
    if ui_mode(uid) == "image_prompt":
        free, premium = get_user_data(uid)
        paid, bonus, tts_free, _chat, image_left, _show = get_balance(uid)

        # Цена картинки — по качеству (IMAGE_QUALITY / IMAGE_PRICES_RUB).
        img_price = image_price_rub()

        # Легаси-Premium (старая подписка) ходит без списания.
        spend_amount = 0.0
        daily_free = True
        if premium:
            pass
        elif image_left > 0 and not guard_on():
            pass
        elif paid + bonus >= img_price:
            daily_free = False
            spend_amount = img_price
        else:
            bot.reply_to(
                message, tr(uid, free_blocked_key(uid)), reply_markup=balance_open_markup(uid)
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
        force_date = selected_cal_date.pop(uid, None)
        counts = save_ai_result(uid, result, use_voice_limit=False, force_date=force_date)
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass

        follow = types.InlineKeyboardMarkup()
        follow.add(types.InlineKeyboardButton(
            tr(uid, "btn_goto_my_tasks"), callback_data="planner:goto_folders"
        ))

        # Одно сообщение: подтверждение + актуальные события с кнопками
        # напоминания/удаления. Раньше здесь был поток из 3+ сообщений.
        if result.get("new_events"):
            event_text, event_markup = upcoming_events_text(uid)
            if event_markup is not None:
                bot.send_message(
                    message.chat.id,
                    tr(uid, "task_added") + "\n\n" + event_text,
                    reply_markup=event_markup,
                )
            else:
                bot.send_message(message.chat.id, tr(uid, "task_added"), reply_markup=follow)
        else:
            bot.send_message(message.chat.id, tr(uid, "task_added"), reply_markup=follow)

        # Напоминания к задачам недоступны без положительного баланса.
        send_task_reminder_upsell(message.chat.id, uid, counts[4])

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

            # Напоминания по задачам (папка + опциональная дата/время).
            with db_lock:
                conn = get_db()
                try:
                    task_rows = conn.execute(
                        """SELECT task_id, user_id, task_text, category
                              FROM tasks
                             WHERE remind_utc IS NOT NULL AND remind_sent=0
                               AND is_completed=0 AND remind_utc<=?
                             ORDER BY remind_utc LIMIT 100""",
                        (now,),
                    ).fetchall()
                    task_ids = [row["task_id"] for row in task_rows]
                    if task_ids:
                        placeholders = ",".join("?" for _ in task_ids)
                        conn.execute(
                            f"UPDATE tasks SET remind_sent=1 WHERE task_id IN ({placeholders})",
                            task_ids,
                        )
                        conn.commit()
                finally:
                    conn.close()

            for row in task_rows:
                uid = row["user_id"]
                try:
                    bot.send_message(
                        uid,
                        tr(
                            uid,
                            "task_reminder",
                            title=row["task_text"],
                            category=row["category"] or "",
                        ),
                    )
                except Exception:
                    logger.exception("Task reminder delivery failed for user %s", uid)

        except Exception:
            logger.exception("Reminder worker error")

        time.sleep(30)


threading.Thread(target=reminder_worker, daemon=True).start()


# ============================================================
# 12. ПАПКИ, НАСТРОЙКИ И ТЕКСТОВЫЕ КОМАНДЫ
# ============================================================

@bot.message_handler(commands=["new_category"])
def new_category_command(message):
    begin_input(message.from_user.id, "category")
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
    enter_mode(uid, "chat")
    bot.send_message(
        message.chat.id, tr(uid, "chat_started"), reply_markup=main_keyboard(uid)
    )


@bot.message_handler(commands=["image"])
def image_command(message):
    """Быстрый вход в режим генерации картинок из команды."""
    uid = message.from_user.id
    if not oferta_gate(message):
        return
    enter_mode(uid, "image_prompt")
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
    # Диагностика в файл: фиксируем, что текст дошёл и в каком режиме бот был,
    # чтобы при «молчании» отличить недоставку сообщения от сбоя внутри ветки.
    logger.info("text uid=%s mode=%r text=%r", uid, ui_mode(uid), text[:40])

    # Антифлуд — раньше любых действий: флудящий не тратит ни квоты, ни ИИ.
    if not flood_gate(message):
        return

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
            and ui_mode(uid) in {"chat", "image_prompt"}
        ):
            leave_mode(uid)

        if text in chat_buttons:
            enter_mode(uid, "chat")
            bot.send_message(
                message.chat.id, tr(uid, "chat_started"), reply_markup=main_keyboard(uid)
            )
            return
        if text in image_buttons:
            enter_mode(uid, "image_prompt")
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


@bot.message_handler(commands=["setbalance"])
def admin_set_balance(message):
    """Установить свой баланс для тестов (только админ).\n\n    /setbalance — показать; /setbalance 500 — платный 500, бонус 0;\n    /setbalance 0 — обнулить; /setbalance 100 50 — платный 100, бонус 50.\n    """
    uid = message.from_user.id
    if uid != ADMIN_ID:
        bot.reply_to(message, tr(uid, "not_admin"))
        return
    parts = message.text.split()
    if len(parts) == 1:
        paid, bonus, _t, _c, _i, _s = get_balance(uid)
        bot.reply_to(message, tr(uid, "setbal_done", paid=f"{paid:.2f}", bonus=f"{bonus:.2f}"))
        return
    try:
        paid_new = float(parts[1].replace(",", "."))
        bonus_new = float(parts[2].replace(",", ".")) if len(parts) >= 3 else 0.0
    except (ValueError, IndexError):
        bot.reply_to(message, tr(uid, "setbal_usage"))
        return
    if not (0 <= paid_new <= 1_000_000 and 0 <= bonus_new <= 1_000_000):
        bot.reply_to(message, tr(uid, "setbal_usage"))
        return
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            conn.execute(
                "UPDATE users SET balance_paid=?, balance_bonus=? WHERE user_id=?",
                (round(paid_new, 2), round(bonus_new, 2), uid),
            )
            conn.commit()
        finally:
            conn.close()
    bot.reply_to(message, tr(uid, "setbal_done", paid=f"{paid_new:.2f}", bonus=f"{bonus_new:.2f}"))


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
        f"💰 Выручка (оплаты): {pay['r']:.2f} ₽\n"
        f"🤖 Расход на ИИ-запросы (себестоимость): {req['c']:.2f} ₽\n"
        f"📈 Прибыль за период: {pay['r'] - req['c']:.2f} ₽",
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
# 14а. ЗАЩИТА: /guard — стоп/пуск бесплатки без деплоя
# ============================================================

@bot.message_handler(commands=["guard"])
def admin_guard(message):
    """Режим защиты (только админ).

    /guard — статус; /guard on — стоп бесплатки (наплыв ботов, админ не в сети);
    /guard off — возврат к штатной работе. Пока guard включён: без подарков,
    без дневных квот, без пробных озвучек — работают только балансы/Premium.
    """
    uid = message.from_user.id
    if uid != ADMIN_ID:
        bot.reply_to(message, tr(uid, "not_admin"))
        return

    parts = (message.text or "").split()
    if len(parts) > 1:
        arg = parts[1].lower()
        if arg in ("on", "1", "вкл", "enable"):
            set_guard(True)
        elif arg in ("off", "0", "выкл", "disable"):
            set_guard(False)

    enabled = guard_on()
    with db_lock:
        conn = get_db()
        try:
            total = int(conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"])
            today = _new_users_today_locked(conn)
        finally:
            conn.close()
    key = "guard_status_on" if enabled else "guard_status_off"
    bot.reply_to(
        message,
        tr(
            uid, key,
            total=total, today=today, cap=MAX_NEW_USERS_PER_DAY,
            burst=SIGNUP_BURST_LIMIT, window=SIGNUP_BURST_WINDOW,
            captcha=SIGNUP_CAPTCHA_LIMIT,
        ),
    )


# ============================================================
# 14б. КАПЧА «Я не робот» ДЛЯ НОВЫХ АККАУНТОВ ВО ВРЕМЯ ВСПЛЕСКА
# ============================================================

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("rc:"))
def signup_captcha_pass(call):
    """Подтверждение «Я не робот»: одноразовый токен с TTL. После нажатия
    стартовые подарки выдаются, если guard выключен и лимит регистраций
    не превышен; иначе аккаунт остаётся без бесплатки (как при /guard on)."""
    uid = call.from_user.id
    try:
        _prefix, rc_uid, nonce = call.data.split(":", 2)
        rc_uid_ok = int(rc_uid) == uid
    except (ValueError, TypeError):
        rc_uid_ok = False

    granted = None  # None — токен не прошёл; True/False — результат выдачи
    if rc_uid_ok:
        with db_lock:
            conn = get_db()
            try:
                raw = meta_get_locked(conn, _captcha_key_locked(uid), "")
                exp_s, sep, stored = raw.partition(":")
                if (
                    sep
                    and stored == nonce
                    and exp_s.isdigit()
                    and int(time.time()) <= int(exp_s)
                ):
                    meta_set_locked(conn, _captcha_key_locked(uid), "")
                    guard = meta_get_locked(conn, "guard_mode") == "1"
                    cap_ok = _new_users_today_locked(conn) <= MAX_NEW_USERS_PER_DAY
                    granted = bool(not guard and cap_ok)
                    if granted:
                        conn.execute(
                            """UPDATE users SET free_off = 0,
                                      tts_free_left = ?, daily_image_left = ?,
                                      daily_voice_left = ?, free_voice_left = ?,
                                      free_day = ''
                               WHERE user_id = ?""",
                            (
                                FREE_TTS_TRIES, FREE_IMAGES_ONCE,
                                FREE_VOICE_TASKS_ONCE, FREE_VOICE_TASKS_ONCE, uid,
                            ),
                        )
                    conn.commit()
            finally:
                conn.close()

    if granted is None:
        try:
            bot.answer_callback_query(call.id, tr(uid, "captcha_expired"), show_alert=True)
        except Exception:
            pass
        return
    try:
        bot.answer_callback_query(call.id, tr(uid, "captcha_toast_ok"))
    except Exception:
        pass
    chat_id = call.message.chat.id if call.message else uid
    if call.message:
        try:
            bot.edit_message_text(
                tr(uid, "captcha_done"), chat_id, call.message.message_id
            )
        except Exception:
            pass
    if granted:
        bot.send_message(
            chat_id,
            tr(
                uid, "captcha_granted",
                chats=FREE_CHATS_PER_DAY, img=FREE_IMAGES_ONCE,
                voice=FREE_VOICE_TASKS_ONCE, tts=FREE_TTS_TRIES,
            ),
        )
    else:
        # guard включён либо лимит регистраций превышен — подарков нет.
        bot.send_message(chat_id, tr(uid, free_blocked_key(uid)))


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

    # Планировщик Pro: planner_pro:30 → продлить календарь без ИИ.
    if payload.startswith("planner_pro:"):
        try:
            days = int(payload.split(":", 1)[1])
        except ValueError:
            days = PLANNER_DAYS
        _extend_planner(uid, days)
        with db_lock:
            conn = get_db()
            try:
                conn.execute(
                    """INSERT INTO payments(user_id, amount_stars, amount_rub, payment_system)
                       VALUES (?, ?, ?, 'Telegram Stars')""",
                    (uid, payment.total_amount, float(PLANNER_PRICE_RUB)),
                )
                conn.commit()
            finally:
                conn.close()
        bot.send_message(uid, tr(uid, "planner_done", days=days))
        if ADMIN_ID:
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"🗓 Планировщик Pro: user_id={uid}, +{days} дн., "
                    f"звёзды={payment.total_amount}",
                )
            except Exception:
                logger.exception("Не удалось уведомить администратора о Планировщике")
        return

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

def ai_chat_reply(uid, text, max_tokens=None):
    """Ответ текстовой нейросети (модель CHAT_MODEL через OpenAI-совместимый API).

    max_tokens ограничивает объём ответа: бесплатный чат — FREE_ANSWER_MAX_TOKENS,
    платящий — PAID_ANSWER_MAX_TOKENS, лаконичный режим — SHORT_ANSWER_MAX_TOKENS.
    """
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
            "max_tokens": int(max_tokens or PAID_ANSWER_MAX_TOKENS),
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
    # usage нужен вызывающему для пофактового расчёта цены чата.
    return data["choices"][0]["message"]["content"].strip(), usage


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
            # quality выбирает IMAGE_QUALITY; цена для пользователя и
            # себестоимость берутся из IMAGE_PRICES_RUB / IMAGE_COSTS_RUB.
            "quality": IMAGE_QUALITY,
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

    log_ai_request(
        uid, "image", cost_rub=IMAGE_COSTS_RUB.get(IMAGE_QUALITY, 1.5)
    )
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


@bot.callback_query_handler(func=lambda call: call.data == "chat:speak")
def chat_speak(call):
    """Кнопка «🔊 Озвучить» под ответом ИИ — присылает ответ голосом.

    Первое прослушивание генерируется и оплачивается один раз; повторные
    того же ответа переотправляются по сохранённому file_id — без вызова
    TTS-API и без списаний (ни у пользователя, ни себестоимости у нас).
    """
    uid = call.from_user.id
    answer = last_answer.get(uid)
    if not answer:
        bot.answer_callback_query(call.id, "Нет текста для озвучки.")
        return

    # Повторное прослушивание того же ответа — бесплатно.
    answer_hash = _tts_answer_hash(answer)
    cached_hash, cached_file_id = get_tts_cache(uid)
    if cached_hash == answer_hash and cached_file_id:
        try:
            bot.send_voice(call.message.chat.id, cached_file_id)
            bot.answer_callback_query(call.id, "🔊")
            return
        except Exception:
            logger.warning("TTS cache file_id failed for user %s — regenerating", uid)
            set_tts_cache(uid, "", "")

    paid, bonus, tts_free, _chat, _img, _show = get_balance(uid)
    tts_cost = tts_price_rub(answer)
    use_trial = tts_free > 0 and not guard_on()
    use_paid = not use_trial and (paid + bonus) >= tts_cost
    if not (TTS_API_URL and TTS_API_KEY and (use_trial or use_paid)):
        bot.answer_callback_query(call.id, "🔇 Озвучка недоступна — нужен баланс.")
        return

    bot.answer_callback_query(call.id, "🔊 Озвучиваю…")
    path = os.path.join(tempfile.gettempdir(), f"voiceplan_tts_{uid}_{uuid.uuid4().hex}.ogg")
    try:
        if synthesize_speech(answer, path):
            with open(path, "rb") as audio:
                sent = bot.send_voice(call.message.chat.id, audio)
            # Запоминаем file_id: повторные прослушивания будут бесплатными.
            try:
                set_tts_cache(uid, answer_hash, sent.voice.file_id)
            except Exception:
                logger.exception("Не удалось сохранить кэш озвучки для %s", uid)
            if use_trial:
                consume_tts_trial(uid)
            else:
                spend_from_balance(uid, tts_cost)
                maybe_receipt(uid, tts_cost, "spend_tts")
            log_ai_request(
                uid,
                "tts",
                cost_rub=round(len(answer[:3500]) / 1000 * TTS_PRICE_RUB_PER_1K, 3),
            )
        else:
            bot.send_message(call.message.chat.id, tr(uid, "api_error"))
    except Exception:
        logger.exception("chat:speak TTS failed for user %s", uid)
        bot.send_message(call.message.chat.id, tr(uid, "api_error"))
    finally:
        if os.path.exists(path):
            os.remove(path)


@bot.callback_query_handler(func=lambda call: call.data == "chat:exit")
def chat_exit(call):
    uid = call.from_user.id
    leave_mode(uid)
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


@bot.callback_query_handler(func=lambda call: call.data == "bal:planner")
def bal_planner(call):
    """Экран «🗓 Планировщик Pro» — календарь и напоминания без ИИ."""
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    text, markup = planner_screen(uid)
    _edit_or_send(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data == "bal:planner_buy")
def bal_planner_buy(call):
    """Инвойс Telegram Stars на Планировщик Pro (календарь без ИИ)."""
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="VoicePlan — Планировщик Pro",
        description=f"Календарь и напоминания на {PLANNER_DAYS} дней, без ИИ.",
        invoice_payload=f"planner_pro:{PLANNER_DAYS}",
        provider_token="",
        currency="XTR",
        prices=[types.LabeledPrice(label=f"Планировщик {PLANNER_DAYS} дн.", amount=PLANNER_PRICE_RUB)],
    )


def _extend_planner(uid, days):
    """Продлить Планировщик Pro на days дней. Возвращает новую дату (ISO)."""
    current = planner_until(uid)
    now = datetime.now(timezone.utc)
    base = current if (current and current > now) else now
    new_until = base + timedelta(days=days)
    text = new_until.isoformat(timespec="seconds")
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            conn.execute("UPDATE users SET planner_until=? WHERE user_id=?", (text, uid))
            conn.commit()
        finally:
            conn.close()
    return text


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
            images=FREE_IMAGES_ONCE,
            tts=FREE_TTS_TRIES,
            voice=FREE_VOICE_TASKS_ONCE,
        ),
    )


@bot.callback_query_handler(func=lambda call: call.data == "bal:how")
def bal_how(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        tr(
            uid,
            "how_balance_text",
            p_chat_min=f"{CHAT_MIN_RUB:g}",
            p_chat_max=f"{CHAT_MAX_RUB:g}",
            p_img_low=f"{IMAGE_PRICES_RUB['low']:g}",
            p_img_med=f"{IMAGE_PRICES_RUB['medium']:g}",
            p_img_high=f"{IMAGE_PRICES_RUB['high']:g}",
            p_tts=f"{PRICE_TTS_RUB_PER_1K:g}",
        ),
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
    port = int(os.environ.get("PORT", "10000"))

    # Защита от повторного запуска. Если порт уже слушается — значит, бот уже
    # запущен в другом окне/сервисе с тем же токеном. Это и есть причина ошибки
    # Telegram 409: два процесса не могут одновременно получать обновления.
    # Вместо бесконечного цикла «409 → жду 15 секунд» выходим сразу с подсказкой.
    if _port_in_use(port):
        logger.error(
            "Порт %s уже занят — похоже, бот уже запущен в другом окне или на "
            "другом сервисе с тем же токеном (ошибка 409). Закройте лишние окна "
            "бота (или остановите службу на Render) и запустите заново.",
            port,
        )
        sys.exit(1)

    try:
        health_server = create_health_server()
    except OSError:
        logger.error(
            "Не удалось занять порт %s — возможно, он занят другой программой. "
            "Укажите другой порт в переменной PORT.", port,
        )
        sys.exit(1)
    threading.Thread(target=health_server.serve_forever, daemon=True).start()

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

