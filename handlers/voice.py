"""Ядро: голос/аудио/текст -> распознавание -> LLM -> задачи, даты, напоминания."""
import logging
import os
import tempfile
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message

import db
from config import DEFAULT_LANG, SYSTEM_CATEGORIES
from locales.texts import t
from services import llm, stt
from services.dates import local_to_utc, parse_iso, utc_to_local
from handlers.tasks import send_dashboard
from utils import safe_md

log = logging.getLogger(__name__)
router = Router()


async def _lang(user_id: int) -> str:
    u = await db.get_user(user_id)
    return u["language"] if u else DEFAULT_LANG


async def _download_file(bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    suffix = os.path.splitext(file.file_path or "")[1] or ".oga"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    await bot.download_file(file.file_path, destination=path)
    return path


async def _process_text(message: Message, user_text: str) -> None:
    uid = message.from_user.id
    u = await db.get_or_create_user(uid, message.from_user.username, DEFAULT_LANG)
    lang = u["language"]

    if not await db.can_process(uid):
        await message.answer(t(lang, "quota_exceeded"), parse_mode="Markdown")
        return

    tasks = await db.get_tasks(uid, only_open=True)
    cats = list(dict.fromkeys(
        (SYSTEM_CATEGORIES.get(lang) or SYSTEM_CATEGORIES["ru"]) + await db.get_category_names(uid)
    ))
    now_local = utc_to_local(datetime.utcnow()).strftime("%Y-%m-%d %H:%M")

    result = await llm.ask(user_text, tasks=tasks, cats=cats, now_str=now_local)
    if result.get("error"):
        await message.answer("\u26A0\uFE0F Ошибка ИИ. Попробуйте позже.")
        return

    for tid in result["completed_task_ids"]:
        task = await db.get_task(tid)
        if task and task["user_id"] == uid:
            await db.set_task_completed(tid, 1)

    default_cat = (SYSTEM_CATEGORIES.get(lang) or SYSTEM_CATEGORIES["ru"])[-1]
    for nt in result["new_tasks"]:
        await db.add_task(uid, nt.get("category") or default_cat, nt["text"])

    created = []
    for rem in result["reminders"]:
        dt_local = parse_iso(rem["datetime"])
        if not dt_local:
            continue
        await db.add_reminder(uid, rem["text"], local_to_utc(dt_local).isoformat(timespec="seconds"))
        created.append((rem["text"], dt_local))

    await db.consume_quota(uid)

    if result["reply"]:
        await message.answer(safe_md(result["reply"]))
    await message.answer(t(lang, "tasks_updated"))
    if created:
        lines = [t(lang, "reminder_list_title")]
        lines += [f"• {safe_md(x)} — {dt.strftime('%d.%m.%Y %H:%M')}" for x, dt in created]
        await message.answer("\n".join(lines), parse_mode="Markdown")
    await send_dashboard(message, uid)


@router.message(F.voice | F.audio)
async def on_voice(message: Message):
    uid = message.from_user.id
    u = await db.get_or_create_user(uid, message.from_user.username, DEFAULT_LANG)
    lang = u["language"]
    if not await db.can_process(uid):
        await message.answer(t(lang, "quota_exceeded"), parse_mode="Markdown")
        return

    status = await message.answer(t(lang, "processing"))
    path = None
    try:
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        path = await _download_file(message.bot, file_id)
        text = await stt.transcribe(path)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка распознавания речи: %s", e)
        await status.edit_text(t(lang, "nothing_recognized"))
        return
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    if not text:
        await status.edit_text(t(lang, "nothing_recognized"))
        return
    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass
    await _process_text(message, text)


# Всегда последним: любой текст (кроме команд) трактуем как задачу.
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    await _process_text(message, message.text)
