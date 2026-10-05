"""Админ-команды: статистика и выдача разборов."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import db
from config import ADMIN_ID
from locales.texts import t

router = Router()


def _is_admin(message: Message) -> bool:
    return bool(ADMIN_ID) and message.from_user.id == ADMIN_ID


@router.message(Command("stats_all"))
async def stats_all(message: Message):
    if not _is_admin(message):
        return
    total, premium, revenue = await db.stats_all()
    await message.answer(f"\U0001F4CA Всего: {total}\n\u2B50 Премиум: {premium}\n\U0001F4B0 Доход: {revenue} \u2B50")


@router.message(Command("stats_period"))
async def stats_period(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
    n = await db.stats_new_users(days)
    await message.answer(f"\U0001F4C8 За {days} дн.: {n} новых пользователей")


@router.message(Command("grant"))
async def grant(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: /grant <user_id> <amount>")
        return
    uid, amount = int(parts[1]), int(parts[2])
    await db.add_free(uid, amount)
    await message.answer(t("ru", "grant_ok", uid=uid, amount=amount))
    try:
        await message.bot.send_message(uid, f"\U0001F381 Вам начислено: +{amount} разборов!")
    except Exception:  # noqa: BLE001
        pass
