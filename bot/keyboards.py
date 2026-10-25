"""Клавиатуры бота."""

from __future__ import annotations

from typing import Iterable, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from . import texts
from .utils import format_dt, humanize_interval

BTN_REPORT = "📨 Передать сведения"
BTN_URGENT = "🆘 Срочно"
BTN_HELP = "ℹ️ Помощь и правила"
BTN_ADMIN = "🛠 Админ-панель"


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=BTN_REPORT))
    builder.row(KeyboardButton(text=BTN_URGENT), KeyboardButton(text=BTN_HELP))
    if is_admin:
        builder.row(KeyboardButton(text=BTN_ADMIN))
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Напишите сообщение…")


def categories() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in texts.CATEGORIES.items():
        builder.button(text=label, callback_data=f"report:cat:{key}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="report:cancel"))
    return builder.as_markup()


def cancel_only(callback_data: str = "report:cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]]
    )


def report_card(anonymous: bool) -> InlineKeyboardMarkup:
    toggle_text = "👤 Показать моё имя" if anonymous else "🕶 Скрыть моё имя"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить администрации", callback_data="report:send")],
            [InlineKeyboardButton(text=toggle_text, callback_data="report:anon")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="report:cancel")],
        ]
    )


def after_report() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Отправить ещё", callback_data="report:new")],
        ]
    )


def report_actions(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Обработано", callback_data=f"rep:done:{report_id}"),
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"rep:reply:{report_id}"),
            ],
            [InlineKeyboardButton(text="🚫 Спам", callback_data=f"rep:spam:{report_id}")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
        InlineKeyboardButton(text="📨 Обращения", callback_data="adm:reports:0"),
    )
    builder.row(InlineKeyboardButton(text="📢 Рассылки", callback_data="adm:bc"))
    builder.row(
        InlineKeyboardButton(text="📥 Экспорт CSV", callback_data="adm:export"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:menu"),
    )
    return builder.as_markup()



def reports_list(
    rows: Sequence[dict], page: int, pages: int, utc_offset: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        mark = texts.STATUS_LABELS.get(row["status"], "").split(" ")[0]
        category = texts.CATEGORIES.get(row["category"], row["category"]).split(" ", 1)[-1]
        builder.button(
            text=(
                f"{mark} #{row['id']} · "
                f"{format_dt(row['created_at'], utc_offset, with_date=False)} · {category}"
            ),
            callback_data=f"adm:rep:{row['id']}:{page}",
        )
    builder.adjust(1)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Новее", callback_data=f"adm:reports:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Старее ➡️", callback_data=f"adm:reports:{page + 1}"))
    builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu"))
    return builder.as_markup()


def report_view(row: dict, page: int) -> InlineKeyboardMarkup:
    report_id = row["id"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Обработано", callback_data=f"rep:done:{report_id}"),
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"rep:reply:{report_id}"),
            ],
            [
                InlineKeyboardButton(text="🚫 Спам", callback_data=f"rep:spam:{report_id}"),
                InlineKeyboardButton(text="👁 Контент", callback_data=f"adm:content:{report_id}"),
            ],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"adm:reports:{page}")],
        ]
    )


def broadcasts_list(rows: Iterable[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        periodic = row["kind"] == texts.BROADCAST_MODE_PERIODIC and row["interval_seconds"]
        schedule = (
            f"каждые {humanize_interval(row['interval_seconds'])}" if periodic else "однократно"
        )
        icon = "▶️" if row["enabled"] else "⏸"
        builder.button(
            text=f"{icon} #{row['id']} · {schedule}",
            callback_data=f"adm:bc:view:{row['id']}",
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="➕ Создать рассылку", callback_data="adm:bc:new"))
    builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu"))
    return builder.as_markup()


def broadcast_view(row: dict) -> InlineKeyboardMarkup:
    broadcast_id = row["id"]
    toggle = InlineKeyboardButton(
        text="⏸ Пауза" if row["enabled"] else "▶️ Включить",
        callback_data=f"adm:bc:toggle:{broadcast_id}",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Отправить сейчас", callback_data=f"adm:bc:run:{broadcast_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👁 Показать контент", callback_data=f"adm:bc:show:{broadcast_id}"
                ),
                toggle,
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:bc:del:{broadcast_id}")],
            [InlineKeyboardButton(text="⬅️ К рассылкам", callback_data="adm:bc")],
        ]
    )


def broadcast_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Однократно сейчас", callback_data="adm:bc:mode:once")],
            [InlineKeyboardButton(text="🔁 Периодически", callback_data="adm:bc:mode:periodic")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:bc:cancel")],
        ]
    )


def broadcast_intervals() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, seconds in texts.BROADCAST_INTERVALS:
        builder.button(text=label, callback_data=f"adm:bc:int:{seconds}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="✍️ Свой интервал", callback_data="adm:bc:int:custom"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="adm:bc:cancel"))
    return builder.as_markup()


def broadcast_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Создать и отправить сейчас", callback_data="adm:bc:make:now"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Создать по расписанию", callback_data="adm:bc:make:later"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:bc:cancel")],
        ]
    )


def broadcast_delete_confirm(broadcast_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить", callback_data=f"adm:bc:delok:{broadcast_id}"
                ),
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm:bc:view:{broadcast_id}"),
            ]
        ]
    )


def back_to_menu(callback_data: str = "adm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data=callback_data)]]
    )
