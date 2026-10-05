"""Небольшие утилиты для безопасного вывода пользовательского текста."""

# Замена Markdown-спецсимволов, чтобы Telegram не падал с "can't parse entities".
_MD_TRANSLATION = str.maketrans({
    "*": "•",
    "_": "‑",      # неразрывный дефис
    "`": "'",
    "[": "(",
    "]": ")",
})


def safe_md(text: str) -> str:
    """Нейтрализовать Markdown-спецсимволы в пользовательском тексте."""
    return (text or "").translate(_MD_TRANSLATION)


def truncate(text: str, limit: int = 40) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
