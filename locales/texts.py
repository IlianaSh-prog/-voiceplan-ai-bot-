"""Мультиязычные тексты интерфейса (RU/EN)."""

TEXTS = {}

TEXTS["ru"] = {
    "welcome": (
        "\U0001F44B **Здравствуйте, {name}!**\n\n"
        "Я — ваш персональный ИИ-органайзер **VoicePlan AI**.\n"
        "\U0001F399 Наговорите задачи голосом — я составлю таблицу, "
        "запишу даты и поставлю напоминания.\n"
        "\U0001F381 Бесплатных разборов: {free} шт."
    ),
    "menu.tasks": "\U0001F4CA Моя таблица",
    "menu.folders": "\U0001F4C2 По папкам",
    "menu.new_cat": "\u2795 Новая папка",
    "menu.reminders": "\u23F0 Напоминания",
    "menu.premium": "\u2B50 Премиум",
    "menu.support": "\U0001F4AC Поддержка",
    "menu.lang": "\U0001F310 Язык / Language",
    "empty_tasks": "\U0001F4ED **Список пуст.**\nНаговорите или напишите дела.",
    "dash_title": "\U0001F4CB **ВАША ТАБЛИЦА ДЕЛ:**\n\n",
    "folders_title": "\U0001F4C2 **ЗАДАЧИ ПО ПАПКАМ:**\n\n",
    "folders_empty": "Папки пока пусты. Наговорите задачи!",
    "new_cat_prompt": "\u270D\uFE0F **Введите название новой папки** (можно с эмодзи):",
    "new_cat_success": "\u2705 Папка **«{cat}»** добавлена!",
    "support": "\U0001F4AC **Поддержка:** {support}\nПишите по любым вопросам.",
    "limits": (
        "\u2B50 **ВАШ ТАРИФ**\n"
        "Статус: **{status}**\n"
        "\U0001F399 Осталось бесплатных разборов: **{free}**\n\n"
        "\U0001F4C4 [Публичная оферта]({offer})"
    ),
    "lang_changed": "\U0001F310 Язык изменён на Русский \U0001F1F7\U0001F1FA",
    "quota_exceeded": (
        "\u26A0\uFE0F **Бесплатные разборы закончились.**\n"
        "Оформите Премиум, чтобы продолжить без ограничений."
    ),
    "processing": "\U0001F3A7 Распознаю и разбираю...",
    "nothing_recognized": "\u26A0\uFE0F Не удалось распознать речь. Попробуйте ещё раз.",
    "tasks_updated": "\u2705 Готово!",
    "premium_info": (
        "\u2B50 **VoicePlan Premium**\n\n"
        "\u2022 неограниченные голосовые разборы\n"
        "\u2022 напоминания без лимита\n"
        "\u2022 свои папки и экспорт\n\n"
        "Цена: **{price} \u2B50/мес**"
    ),
    "premium_buy": "\u2B50 Купить Премиум",
    "premium_active": "\u2705 Премиум активирован! Спасибо за покупку.",
    "reminder_created": "\u23F0 Напоминание создано: **{text}**\nКогда: `{when}`",
    "reminder_list_title": "\u23F0 **АКТИВНЫЕ НАПОМИНАНИЯ:**\n\n",
    "reminder_empty": "Активных напоминаний нет.",
    "reminder_fired": "\u23F0 **Напоминание:** {text}",
    "remind_usage": "\u2139\uFE0F Использование: `/remind 31.12 18:00 текст`",
    "remind_bad_date": "\u26A0\uFE0F Не понял дату. Формат: `ДД.ММ.ГГГГ ЧЧ:ММ` или `ДД.ММ ЧЧ:ММ`.",
    "not_admin": "\u26D4 Недостаточно прав.",
    "grant_ok": "\u2705 Пользователю {uid} начислено +{amount} разборов.",
}

TEXTS["en"] = {
    "welcome": (
        "\U0001F44B **Hello, {name}!**\n\n"
        "I am your **VoicePlan AI** organizer.\n"
        "\U0001F399 Send a voice note — I'll build your task board, "
        "capture dates and set reminders.\n"
        "\U0001F381 Free runs left: {free}"
    ),
    "menu.tasks": "\U0001F4CA My board",
    "menu.folders": "\U0001F4C2 Folders",
    "menu.new_cat": "\u2795 New folder",
    "menu.reminders": "\u23F0 Reminders",
    "menu.premium": "\u2B50 Premium",
    "menu.support": "\U0001F4AC Support",
    "menu.lang": "\U0001F310 Language / Язык",
    "empty_tasks": "\U0001F4ED **Your list is empty.**\nSend a voice note or type tasks.",
    "dash_title": "\U0001F4CB **YOUR TASK BOARD:**\n\n",
    "folders_title": "\U0001F4C2 **TASKS BY FOLDER:**\n\n",
    "folders_empty": "No folders yet. Send a voice note!",
    "new_cat_prompt": "\u270D\uFE0F **Enter a new folder name** (emoji allowed):",
    "new_cat_success": "\u2705 Folder **\"{cat}\"** added!",
    "support": "\U0001F4AC **Support:** {support}",
    "limits": (
        "\u2B50 **YOUR PLAN**\n"
        "Status: **{status}**\n"
        "\U0001F399 Free runs left: **{free}**\n\n"
        "\U0001F4C4 [Terms]({offer})"
    ),
    "lang_changed": "\U0001F310 Language set to English \U0001F1FA\U0001F1F8",
    "quota_exceeded": (
        "\u26A0\uFE0F **Your free runs are used up.**\n"
        "Get Premium to continue without limits."
    ),
    "processing": "\U0001F3A7 Recognizing...",
    "nothing_recognized": "\u26A0\uFE0F Could not recognize speech. Try again.",
    "tasks_updated": "\u2705 Done!",
    "premium_info": (
        "\u2B50 **VoicePlan Premium**\n\n"
        "\u2022 unlimited voice runs\n"
        "\u2022 unlimited reminders\n"
        "\u2022 custom folders & export\n\n"
        "Price: **{price} \u2B50/mo**"
    ),
    "premium_buy": "\u2B50 Buy Premium",
    "premium_active": "\u2705 Premium activated! Thank you.",
    "reminder_created": "\u23F0 Reminder set: **{text}**\nAt: `{when}`",
    "reminder_list_title": "\u23F0 **ACTIVE REMINDERS:**\n\n",
    "reminder_empty": "No active reminders.",
    "reminder_fired": "\u23F0 **Reminder:** {text}",
    "remind_usage": "\u2139\uFE0F Usage: `/remind 31.12 18:00 text`",
    "remind_bad_date": "\u26A0\uFE0F Could not parse date. Format: `DD.MM.YYYY HH:MM` or `DD.MM HH:MM`.",
    "not_admin": "\u26D4 Not allowed.",
    "grant_ok": "\u2705 Granted +{amount} runs to user {uid}.",
}


def t(lang: str, key: str, **kw) -> str:
    """Вернуть текст по ключу с фолбэком на русский."""
    lang = lang if lang in TEXTS else "ru"
    template = TEXTS[lang].get(key) or TEXTS["ru"].get(key, key)
    try:
        return template.format(**kw)
    except (KeyError, IndexError):
        return template

