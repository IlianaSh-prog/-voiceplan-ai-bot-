"""Премиум и оплата через Telegram Stars (XTR)."""
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import db
from config import ADMIN_ID, DEFAULT_LANG, PRICE_STARS
from locales.texts import t
from handlers.keyboards import premium_menu

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == "menu:premium")
async def cb_premium(call: CallbackQuery):
    u = await db.get_or_create_user(call.from_user.id, call.from_user.username, DEFAULT_LANG)
    lang = u["language"]
    await call.message.answer(
        t(lang, "premium_info", price=PRICE_STARS),
        reply_markup=premium_menu(lang),
        parse_mode="Markdown",
    )
    await call.answer()


@router.callback_query(F.data == "premium:buy")
async def cb_buy(call: CallbackQuery):
    await call.message.answer_invoice(
        title="VoicePlan Premium",
        description=t("en", "premium_info", price=PRICE_STARS).replace("*", ""),
        payload=f"premium:{call.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Premium 30d", amount=PRICE_STARS)],
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message):
    uid = message.from_user.id
    await db.get_or_create_user(uid, message.from_user.username, DEFAULT_LANG)
    await db.set_premium(uid, 1)
    amount = message.successful_payment.total_amount
    await db.record_payment(uid, amount, "Stars")
    lang = (await db.get_user(uid))["language"]
    await message.answer(t(lang, "premium_active"))
    if ADMIN_ID:
        try:
            await message.bot.send_message(ADMIN_ID, f"\U0001F4B0 Оплата: {amount} \u2B50 от {uid}")
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось уведомить админа: %s", e)
