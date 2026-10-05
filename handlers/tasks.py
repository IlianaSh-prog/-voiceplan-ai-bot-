"""Таблица задач, папки и создание категории."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import db
from config import DEFAULT_LANG
from locales.texts import t
from handlers.keyboards import main_menu, tasks_menu
from utils import safe_md

router = Router()

INBOX = "\U0001F4E5"  # 📥 — категория по умолчанию


class NewCategory(StatesGroup):
    waiting_name = State()


async def _lang(user_id: int) -> str:
    u = await db.get_user(user_id)
    return u["language"] if u else DEFAULT_LANG


async def send_dashboard(message: Message, user_id: int) -> None:
    lang = await _lang(user_id)
    tasks = await db.get_tasks(user_id, only_open=True)
    if not tasks:
        await message.answer(t(lang, "empty_tasks"), parse_mode="Markdown",
                             reply_markup=main_menu(lang))
        return
    lines = [t(lang, "dash_title")]
    for task in tasks:
        lines.append(f"• {safe_md(task['category'] or INBOX)} — {safe_md(task['text'])}")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=tasks_menu(tasks, lang))


async def send_folders(message: Message, user_id: int) -> None:
    lang = await _lang(user_id)
    tasks = await db.get_tasks(user_id, only_open=True)
    if not tasks:
        await message.answer(t(lang, "folders_empty"), reply_markup=main_menu(lang))
        return
    groups: dict[str, list[str]] = {}
    for task in tasks:
        groups.setdefault(task["category"] or INBOX, []).append(task["text"])
    lines = [t(lang, "folders_title")]
    for cat, items in groups.items():
        lines.append(f"*{safe_md(cat)}*")
        lines.extend(f"  – {safe_md(it)}" for it in items)
        lines.append("")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu(lang))


@router.callback_query(F.data == "menu:tasks")
async def cb_tasks(call: CallbackQuery):
    await send_dashboard(call.message, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "menu:folders")
async def cb_folders(call: CallbackQuery):
    await send_folders(call.message, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "menu:newcat")
async def cb_new_category(call: CallbackQuery, state: FSMContext):
    lang = await _lang(call.from_user.id)
    await state.set_state(NewCategory.waiting_name)
    await call.message.answer(t(lang, "new_cat_prompt"), parse_mode="Markdown")
    await call.answer()


@router.message(NewCategory.waiting_name)
async def process_new_category(message: Message, state: FSMContext):
    lang = await _lang(message.from_user.id)
    name = (message.text or "").strip()[:60]
    await state.clear()
    if not name:
        await message.answer(t(lang, "new_cat_prompt"), parse_mode="Markdown")
        return
    await db.add_category(message.from_user.id, name)
    await message.answer(t(lang, "new_cat_success", cat=safe_md(name)), parse_mode="Markdown")


@router.callback_query(F.data.startswith("task:done:"))
async def cb_task_done(call: CallbackQuery):
    try:
        task_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer()
        return
    task = await db.get_task(task_id)
    if task and task["user_id"] == call.from_user.id:
        await db.set_task_completed(task_id, 1)
    await call.answer("\u2705")
    await send_dashboard(call.message, call.from_user.id)
