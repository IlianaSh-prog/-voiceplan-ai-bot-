"""Слой доступа к данным (SQLite через aiosqlite). Каждая операция — своё соединение."""
import aiosqlite
from datetime import datetime, timedelta
from config import DB_PATH, FREE_LIMIT

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    language    TEXT DEFAULT 'ru',
    is_premium  INTEGER DEFAULT 0,
    free_left   INTEGER DEFAULT 10,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    category     TEXT,
    text         TEXT NOT NULL,
    is_completed INTEGER DEFAULT 0,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    remind_at  TEXT NOT NULL,
    is_sent    INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER,
    system     TEXT,
    created_at TEXT
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def _fetchone(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _fetchall(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def _execute(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        await db.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- users
async def get_or_create_user(user_id: int, username: str | None = None, lang: str = "ru"):
    user = await _fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    if user:
        return user
    await _execute(
        "INSERT INTO users (user_id, username, language, free_left, created_at) VALUES (?,?,?,?,?)",
        (user_id, username, lang, FREE_LIMIT, _now()),
    )
    return await _fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def get_user(user_id: int):
    return await _fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def set_language(user_id: int, lang: str) -> None:
    await _execute("UPDATE users SET language=? WHERE user_id=?", (lang, user_id))


async def set_premium(user_id: int, value: int = 1) -> None:
    await _execute("UPDATE users SET is_premium=? WHERE user_id=?", (value, user_id))


async def add_free(user_id: int, amount: int) -> None:
    await _execute("UPDATE users SET free_left = free_left + ? WHERE user_id=?", (amount, user_id))


async def can_process(user_id: int) -> bool:
    user = await get_user(user_id)
    if not user:
        return False
    return bool(user["is_premium"]) or user["free_left"] > 0


async def consume_quota(user_id: int) -> None:
    user = await get_user(user_id)
    if user and not user["is_premium"] and user["free_left"] > 0:
        await _execute("UPDATE users SET free_left = free_left - 1 WHERE user_id=?", (user_id,))


# ---------------------------------------------------------------- tasks
async def add_task(user_id: int, category: str | None, text: str) -> int:
    return await _execute(
        "INSERT INTO tasks (user_id, category, text, created_at) VALUES (?,?,?,?)",
        (user_id, category, text, _now()),
    )


async def get_tasks(user_id: int, only_open: bool = True):
    q = "SELECT * FROM tasks WHERE user_id=?"
    if only_open:
        q += " AND is_completed=0"
    q += " ORDER BY is_completed ASC, id DESC"
    return await _fetchall(q, (user_id,))


async def get_task(task_id: int):
    return await _fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))


async def set_task_completed(task_id: int, value: int) -> None:
    await _execute("UPDATE tasks SET is_completed=? WHERE id=?", (value, task_id))


async def delete_task(task_id: int) -> None:
    await _execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ---------------------------------------------------------------- categories
async def get_categories(user_id: int):
    return await _fetchall("SELECT * FROM categories WHERE user_id=? ORDER BY id", (user_id,))


async def get_category_names(user_id: int):
    rows = await get_categories(user_id)
    return [r["name"] for r in rows]


async def add_category(user_id: int, name: str) -> bool:
    try:
        await _execute("INSERT INTO categories (user_id, name) VALUES (?,?)", (user_id, name))
        return True
    except aiosqlite.IntegrityError:
        return False


# ---------------------------------------------------------------- reminders
async def add_reminder(user_id: int, text: str, remind_at_iso: str) -> int:
    return await _execute(
        "INSERT INTO reminders (user_id, text, remind_at, created_at) VALUES (?,?,?,?)",
        (user_id, text, remind_at_iso, _now()),
    )


async def get_user_reminders(user_id: int):
    return await _fetchall(
        "SELECT * FROM reminders WHERE user_id=? AND is_sent=0 ORDER BY remind_at", (user_id,)
    )


async def get_due_reminders(now_iso: str):
    return await _fetchall("SELECT * FROM reminders WHERE is_sent=0 AND remind_at<=?", (now_iso,))


async def mark_reminder_sent(reminder_id: int) -> None:
    await _execute("UPDATE reminders SET is_sent=1 WHERE id=?", (reminder_id,))


async def delete_reminder(user_id: int, reminder_id: int) -> None:
    await _execute("DELETE FROM reminders WHERE id=? AND user_id=?", (reminder_id, user_id))


# ---------------------------------------------------------------- payments / stats
async def record_payment(user_id: int, amount: int, system: str) -> int:
    return await _execute(
        "INSERT INTO payments (user_id, amount, system, created_at) VALUES (?,?,?,?)",
        (user_id, amount, system, _now()),
    )


async def stats_all():
    total = await _fetchone("SELECT COUNT(*) AS c FROM users")
    premium = await _fetchone("SELECT COUNT(*) AS c FROM users WHERE is_premium=1")
    revenue = await _fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM payments")
    return (
        total["c"] if total else 0,
        premium["c"] if premium else 0,
        revenue["s"] if revenue else 0,
    )


async def stats_new_users(days: int) -> int:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    row = await _fetchone("SELECT COUNT(*) AS c FROM users WHERE created_at>=?", (since,))
    return row["c"] if row else 0


