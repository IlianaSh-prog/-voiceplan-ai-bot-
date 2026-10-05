"""Сборка inline-клавиатур."""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from locales.texts import t
from utils import safe_md, truncate


def main_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t(lang, "menu.tasks"), callback_data="menu:tasks"),
            InlineKeyboardButton(text=t(lang, "menu.folders"), callback_data="menu:folders"),
        ],
        [
            InlineKeyboardButton(text=t(lang, "menu.new_cat"), callback_data="menu:newcat"),
            InlineKeyboardButton(text=t(lang, "menu.reminders"), callback_data="menu:reminders"),
        ],
        [InlineKeyboardButton(text=t(lang, "menu.premium"), callback_data="menu:premium")],
        [
            InlineKeyboardButton(text=t(lang, "menu.support"), callback_data="menu:support"),
            InlineKeyboardButton(text=t(lang, "menu.lang"), callback_data="menu:lang"),
        ],
    ])


def tasks_menu(tasks, lang: str) -> InlineKeyboardMarkup:
    rows = []
    for task in tasks[:25]:
        rows.append([InlineKeyboardButton(
            text=f"✅ {truncate(safe_md(task['text']), 40)}",
            callback_data=f"task:done:{task['id']}",
        )])
    rows.append([InlineKeyboardButton(text=t(lang, "menu.tasks"), callback_data="menu:tasks")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def languages_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇺🇸 English", callback_data="lang:en"),
    ]])


def premium_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "premium_buy"), callback_data="premium:buy"),
    ]])
