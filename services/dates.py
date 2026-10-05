"""Разбор дат/времени и конвертация локального времени <-> UTC."""
from datetime import datetime, timedelta

import dateparser

from config import TZ_OFFSET_HOURS

_FORMATS = ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%d.%m %H:%M", "%d.%m")


def local_to_utc(dt: datetime) -> datetime:
    return dt - timedelta(hours=TZ_OFFSET_HOURS)


def utc_to_local(dt: datetime) -> datetime:
    return dt + timedelta(hours=TZ_OFFSET_HOURS)


def parse_when(text: str):
    """Разобрать дату из пользовательского текста ('31.12 18:00', 'завтра в 15:00')."""
    text = (text or "").strip()
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.utcnow().year)
            return dt
        except ValueError:
            continue
    return dateparser.parse(
        text,
        languages=["ru", "en"],
        settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.utcnow()},
    )


def parse_iso(text: str):
    """Разобрать абсолютную дату 'YYYY-MM-DD HH:MM' (из ответа LLM)."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return parse_when(text)
