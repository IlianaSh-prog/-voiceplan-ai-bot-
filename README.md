# VoicePlan AI 🤖🎙️

ИИ-органайзер в Telegram: **наговариваете задачи голосом → бот строит таблицу дел, раскладывает по папкам, записывает даты и ставит напоминания.**

Переписан с нуля на **aiogram 3.x (async)** — вместо синхронного `telebot`.

## ✨ Возможности
- 🎙️ Голос/аудио → текст (**faster-whisper**), чистка слов-паразитов через LLM.
- 📊 Таблица задач, ✅ отметка «сделано» одним тапом.
- 📂 Категории/папки (свои + системные).
- ⏰ **Напоминания** (кнопка + команда `/remind`), переживают перезапуск.
- 📅 Распознавание дат («завтра в 15:00», «31.12 18:00») с учётом часового пояса.
- 🌐 Русский / English.
- 💳 Премиум через **Telegram Stars** (XTR).
- 🛡️ Глобальный обработчик ошибок, антифлуд, таймауты, строгий парсинг JSON.

## 📁 Структура
```
voiceplan-ai/
├── bot.py                # точка входа
├── config.py             # конфиг (всё из .env)
├── db.py                 # SQLite (aiosqlite)
├── utils.py              # safe_md/truncate
├── handlers/             # start, tasks, voice, reminders, payments, admin, keyboards
├── services/             # llm, stt, scheduler, dates
├── middlewares/          # throttle
├── locales/texts.py      # тексты RU/EN
├── requirements.txt
├── Dockerfile
└── .env.example
```

## 🚀 Быстрый старт (Windows / Linux / macOS)

1. **Python 3.11+** и **ffmpeg** (нужен для голосовых).
   - Windows: `winget install Gyan.FFmpeg` или скачать с ffmpeg.org (и добавить в PATH).
   - Linux: `sudo apt install ffmpeg`
   - macOS: `brew install ffmpeg`
2. Создать виртуальное окружение и установить зависимости:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Скопировать `.env.example` → `.env` и заполнить:
   - `TELEGRAM_BOT_TOKEN` — токен от @BotFather
   - `AI_API_KEY` — ключ ProxyAPI (или другого OpenAI-совместимого)
   - `AI_BASE_URL`, `AI_MODEL`
   - `ADMIN_ID` — ваш Telegram ID
   - `SUPPORT_HANDLE`, `OFFER_URL`
4. Запуск:
   ```bash
   python bot.py
   ```
   Первый запуск скачает модель Whisper (~0.5–1 ГБ).

> ⚠️ Команды бота (меню) работают в личке. Для приёма платежей Stars подключите платёжный профиль у @BotFather.

## 🐳 Docker
```bash
docker build -t voiceplan-ai .
docker run --env-file .env -v %cd%/data:/app/data voiceplan-ai
```

## ☁️ Хостинг
- **Российский VPS** (Timeweb, Beget, Reg.ru, Selectel, VDSina) — оплата в рублях.
- ИИ — по API (ProxyAPI/DeepSeek/Qwen/Z.ai), чтобы не держать GPU на сервере.
- Для стартовой модели Whisper на CPU ставьте `WHISPER_MODEL=small`.

## 🔒 Безопасность
- Секреты — только в `.env` (в `.gitignore`).
- Токен/ключи **никому не передавайте**.

## 📝 Что дальше (роадмап)
- Экспорт в Google Calendar / Notion / Obsidian.
- Оплата картой РФ (ЮKassa) как альтернатива Stars.
- Вектор-память и авто-сводки.
