"""Конфигурация VoicePlan AI. Все секреты берутся из переменных окружения (.env)."""
import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Секреты / подключения ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
AI_API_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.proxyapi.ru/openai/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")

# --- Приложение ---
ADMIN_ID = _int("ADMIN_ID", 0)
OFFER_URL = os.environ.get("OFFER_URL", "")
DB_PATH = os.environ.get("DB_PATH", "voiceplan.db")
FREE_LIMIT = _int("FREE_LIMIT", 10)
PRICE_STARS = _int("PRICE_STARS", 199)
SUPPORT_HANDLE = os.environ.get("SUPPORT_HANDLE", "@your_support")
TZ_OFFSET_HOURS = _int("TZ_OFFSET_HOURS", 3)  # локальное время пользователя = UTC + offset (по умолч. МСК)

# --- Whisper ---
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

# --- Локализация ---
DEFAULT_LANG = "ru"
SUPPORTED_LANGS = ("ru", "en")

SYSTEM_CATEGORIES = {
    "ru": ["\U0001F534 Срочно", "\U0001F4C5 Календарь", "\U0001F6D2 Покупки", "\U0001F4A1 Идеи", "\U0001F4E5 Входящие"],
    "en": ["\U0001F534 Urgent", "\U0001F4C5 Calendar", "\U0001F6D2 Groceries", "\U0001F4A1 Ideas", "\U0001F4E5 Inbox"],
}

if not BOT_TOKEN:
    raise SystemExit("[ERROR] Переменная TELEGRAM_BOT_TOKEN не задана (см. .env.example)")
