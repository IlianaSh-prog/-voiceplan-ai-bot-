"""Напоминания: команда /remind, список, конвертация времени."""
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import db
from config import DEFAULT_LANG
from locales.texts import t
from handlers.keyboards import main_menu
from services.dates import parse_when, local_to_utc, utc_to_local
from utils import safe_md

router = Router()


async def _lang(user_id: int) -> str:
    u = await db.get_user(user_id)
    return u["language"] if u else DEFAULT_LANG


async def send_reminders(message: Message, user_id: int) -> None:
    lang = await _lang(user_id)
    rows = await db.get_user_reminders(user_id)
    if not rows:
        await message.answer(t(lang, "reminder_empty"), reply_markup=main_menu(lang))
        return
    lines = [t(lang, "reminder_list_title")]
    for r in rows:
        local = utc_to_local(datetime.fromisoformat(r["remind_at"]))
        lines.append(f"• {safe_md(r['text'])} — {local.strftime('%d.%m.%Y %H:%M')}")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu(lang))


@router.message(Command("remind"))
async def cmd_remind(message: Message):
    await db.get_or_create_user(message.from_user.id, message.from_user.username, DEFAULT_LANG)
    lang = await _lang(message.from_user.id)
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(t(lang, "remind_usage"), parse_mode="Markdown")
        return
    when_local = parse_when(parts[1])
    if not when_local:
        await message.answer(t(lang, "remind_bad_date"), parse_mode="Markdown")
        return
    text = parts[2].strip()
    await db.add_reminder(
        message.from_user.id, text, local_to_utc(when_local).isoformat(timespec="seconds")
    )
    await message.answer(
        t(lang, "reminder_created", text=safe_md(text), when=when_local.strftime("%d.%m.%Y %H:%M")),
        parse_mode="Markdown",
    )


@router.message(Command("reminders"))
async def cmd_reminders(message: Message):
    await db.get_or_create_user(message.from_user.id, message.from_user.username, DEFAULT_LANG)
    await send_reminders(message, message.from_user.id)


@router.callback_query(F.data == "menu:reminders")
async def cb_reminders(call: CallbackQuery):
    await send_reminders(call.message, call.from_user.id)
    await call.answer()
