"""Обёртка над LLM (OpenAI-совместимый эндпоинт, например ProxyAPI).

Ключевая задача — надёжно получить СТРОГИЙ JSON от модели и не упасть,
если та вернула markdown-обёртку или лишний текст.
"""
import json
import re
import logging
import httpx

from config import AI_API_KEY, AI_BASE_URL, AI_MODEL

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

SYSTEM_TEMPLATE = """Ты — VoicePlan AI, ИИ-органайзер задач.
Пользователь диктует дела, напоминания и события голосом или текстом.
Верни СТРОГО валидный JSON (без markdown, без пояснений) вида:
{{
  "reply": "короткий дружелюбный ответ",
  "new_tasks": [{{"category": "папка", "text": "задача"}}],
  "completed_task_ids": [1, 2],
  "reminders": [{{"text": "о чём напомнить", "datetime": "YYYY-MM-DD HH:MM"}}]
}}
Правила:
- category — выбери из списка папок пользователя или придумай новую;
- если пользователь говорит, что дело сделано — добавь id этой задачи в completed_task_ids;
- если в речи есть дата/время — создай reminder с АБСОЛЮТНОЙ датой YYYY-MM-DD HH:MM;
- очищай текст задач от слов-паразитов ("эээ", "ну", "как бы", "короче").
Текущее время (UTC): {now}
Открытые задачи (id: текст): {tasks}
Доступные папки: {cats}
"""


def _build_system(now_str: str, tasks, cats) -> str:
    tasks_str = "; ".join(f"{x['id']}: {x['text']}" for x in tasks) or "нет"
    cats_str = ", ".join(cats) or "нет"
    return SYSTEM_TEMPLATE.format(now=now_str, tasks=tasks_str, cats=cats_str)


def extract_json(text: str) -> dict:
    """Достать JSON из ответа модели, даже если он в ```-обёртке."""
    if not text:
        raise ValueError("empty LLM response")
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def _normalize(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    new_tasks = []
    for x in data.get("new_tasks") or []:
        if isinstance(x, dict) and x.get("text"):
            new_tasks.append({"category": x.get("category") or "", "text": str(x["text"]).strip()})
    completed = []
    for x in data.get("completed_task_ids") or []:
        try:
            completed.append(int(x))
        except (TypeError, ValueError):
            continue
    reminders = []
    for x in data.get("reminders") or []:
        if isinstance(x, dict) and x.get("text") and x.get("datetime"):
            reminders.append({"text": str(x["text"]).strip(), "datetime": str(x["datetime"]).strip()})
    return {
        "reply": str(data.get("reply", "") or "").strip(),
        "new_tasks": new_tasks,
        "completed_task_ids": completed,
        "reminders": reminders,
    }


async def ask(user_text: str, *, tasks=None, cats=None, now_str: str = "") -> dict:
    """Отправить текст в LLM и вернуть нормализованный dict."""
    tasks = tasks or []
    cats = cats or []
    system = _build_system(now_str, tasks, cats)
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    url = f"{AI_BASE_URL}/chat/completions"

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code == 400:  # некоторые модели не поддерживают response_format
                payload.pop("response_format", None)
                r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            log.error("LLM request failed: %s", e)
            return {"reply": "", "new_tasks": [], "completed_task_ids": [], "reminders": [], "error": str(e)}

    try:
        return _normalize(extract_json(content))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("LLM JSON parse failed: %s", e)
        return _normalize({"reply": content})
