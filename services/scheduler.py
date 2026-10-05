"""Планировщик напоминаний на APScheduler.

Каждые 30 секунд проверяет БД на «созревшие» напоминания и отправляет их.
Хранится в памяти процесса; напоминания при этом лежат в SQLite, поэтому
переживают перезапуск бота (сработают сразу после старта, если время прошло).
"""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import db
from locales.texts import t

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def start(bot) -> None:
    scheduler.add_job(_check_reminders, "interval", seconds=30, args=[bot], id="reminders")
    scheduler.start()
    log.info("Планировщик напоминаний запущен.")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def _check_reminders(bot) -> None:
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    try:
        due = await db.get_due_reminders(now_iso)
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось получить напоминания: %s", e)
        return

    for r in due:
        try:
            user = await db.get_user(r["user_id"])
            lang = user["language"] if user else "ru"
            await bot.send_message(r["user_id"], t(lang, "reminder_fired", text=r["text"]))
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось отправить напоминание %s: %s", r["user_id"], e)
        await db.mark_reminder_sent(r["id"])
