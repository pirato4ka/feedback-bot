"""Вспомогательные функции: работа со временем, интервалами и текстом."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 30 * 24 * 60 * 60  # 30 дней


def now_utc() -> datetime:
    """Текущее время в UTC (без микросекунд)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    """Приводит datetime к строке ISO 8601 в UTC (формат пригоден для сравнения строк)."""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_iso() -> str:
    return to_iso(now_utc())


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_dt(value: str | datetime | None, utc_offset: int = 3, with_date: bool = True) -> str:
    """Форматирует время для отображения в часовом поясе общины."""
    if value is None:
        return "—"
    moment = parse_iso(value) if isinstance(value, str) else value
    local = moment.astimezone(timezone(timedelta(hours=utc_offset)))
    return local.strftime("%d.%m.%Y %H:%M") if with_date else local.strftime("%H:%M")


def humanize_interval(seconds: int) -> str:
    """Человекочитаемый интервал: 90 -> '1 ч 30 мин'."""
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} дн."
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч"
    if seconds > 3600:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if seconds % 60 == 0:
        return f"{seconds // 60} мин"
    return f"{seconds} сек"


_UNIT_SECONDS = {
    "m": 60,
    "мин": 60,
    "min": 60,
    "минута": 60,
    "минут": 60,
    "h": 3600,
    "ч": 3600,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
    "d": 86400,
    "д": 86400,
    "день": 86400,
    "дня": 86400,
    "дн": 86400,
    "дней": 86400,
}

_INTERVAL_RE = re.compile(r"^(\d+)\s*([a-zA-Zа-яА-Я]*)$")


def parse_interval(raw: str) -> tuple[int | None, str | None]:
    """Разбирает интервал из текста вида '30', '30m', '2ч', '1 день'.

    Возвращает (секунды, текст ошибки).
    """
    text = raw.strip().lower().replace("ё", "е")
    match = _INTERVAL_RE.fullmatch(text)
    if not match:
        return None, "Не понял интервал. Пример: <code>90</code>, <code>30m</code>, <code>2h</code>, <code>1d</code>."

    amount = int(match.group(1))
    unit = match.group(2) or "m"

    if unit not in _UNIT_SECONDS:
        return None, "Неизвестная единица измерения. Используйте <code>m</code> (минуты), <code>h</code> (часы) или <code>d</code> (дни)."

    seconds = amount * _UNIT_SECONDS[unit]
    if seconds < MIN_INTERVAL_SECONDS:
        return None, f"Минимальный интервал — {humanize_interval(MIN_INTERVAL_SECONDS)}."
    if seconds > MAX_INTERVAL_SECONDS:
        return None, "Максимальный интервал — 30 дней."
    return seconds, None


def truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
