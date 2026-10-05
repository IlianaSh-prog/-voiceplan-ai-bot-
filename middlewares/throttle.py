"""Простой антифлуд-middleware: не более N событий за окно секунд на пользователя."""
import time

from aiogram import BaseMiddleware


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, window: float = 1.0, max_events: int = 8):
        self.window = window
        self.max_events = max_events
        self._buckets: dict[int, list[float]] = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        bucket = [ts for ts in self._buckets.get(user.id, []) if now - ts < self.window]
        if len(bucket) >= self.max_events:
            return None  # тихо игнорируем флуд
        bucket.append(now)
        self._buckets[user.id] = bucket
        return await handler(event, data)
