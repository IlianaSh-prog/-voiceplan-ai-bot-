"""VoicePlan AI — точка входа (aiogram 3.x)."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

import db
from config import BOT_TOKEN
from handlers import admin, payments, reminders, start, tasks, voice
from middlewares.throttle import ThrottleMiddleware
from services import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("voiceplan")


async def main() -> None:
    await db.init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()

    throttle = ThrottleMiddleware()
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)

    # Порядок важен: voice (catch-all по тексту) — строго последним.
    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(tasks.router)
    dp.include_router(reminders.router)
    dp.include_router(payments.router)
    dp.include_router(voice.router)

    @dp.errors()
    async def on_error(event):
        log.exception("Ошибка при обработке апдейта: %s", event.exception)
        return True

    scheduler.start(bot)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("\U0001F680 VoicePlan AI запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")

