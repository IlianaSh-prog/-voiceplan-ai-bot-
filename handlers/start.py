"""Старт, меню, переключение языка, поддержка."""
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import CallbackQuery, Message

import db
from config import DEFAULT_LANG, SUPPORT_HANDLE
from locales.texts import t
from handlers.keyboards import main_menu, languages_menu

router = Router()

MENU_TITLES = {"ru": "Главное меню:", "en": "Main menu:"}


async def user_lang(user_id: int) -> str:
    u = await db.get_user(user_id)
    return u["language"] if u else DEFAULT_LANG


@router.message(CommandStart())
async def cmd_start(message: Message):
    u = await db.get_or_create_user(message.from_user.id, message.from_user.username, DEFAULT_LANG)
    await message.answer(
        t(u["language"], "welcome", name=message.from_user.first_name or "", free=u["free_left"]),
        reply_markup=main_menu(u["language"]),
        parse_mode="Markdown",
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    u = await db.get_or_create_user(message.from_user.id, message.from_user.username, DEFAULT_LANG)
    lang = u["language"]
    await message.answer(MENU_TITLES.get(lang, MENU_TITLES["ru"]), reply_markup=main_menu(lang))


@router.callback_query(F.data == "menu:support")
async def cb_support(call: CallbackQuery):
    lang = await user_lang(call.from_user.id)
    await call.message.answer(t(lang, "support", support=SUPPORT_HANDLE), parse_mode="Markdown")
    await call.answer()


@router.callback_query(F.data == "menu:lang")
async def cb_lang(call: CallbackQuery):
    await call.message.answer("\U0001F310", reply_markup=languages_menu())
    await call.answer()


@router.callback_query(F.data.startswith("lang:"))
async def cb_set_lang(call: CallbackQuery):
    lang = call.data.split(":", 1)[1]
    if lang not in ("ru", "en"):
        await call.answer()
        return
    await db.get_or_create_user(call.from_user.id, call.from_user.username, DEFAULT_LANG)
    await db.set_language(call.from_user.id, lang)
    await call.message.answer(t(lang, "lang_changed"))
    u = await db.get_user(call.from_user.id)
    await call.message.answer(
        t(lang, "welcome", name=call.from_user.first_name or "", free=u["free_left"]),
        reply_markup=main_menu(lang),
        parse_mode="Markdown",
    )
    await call.answer()
