"""Админ-панель: обращения, статистика, ответы и экспорт."""

from __future__ import annotations

import csv
import html
import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import keyboards, texts
from ..config import Config
from ..db import Database
from ..middlewares import IsAdmin, IsAdminOrAdminChat
from ..scheduler import BroadcastScheduler
from ..sender import (
    KIND_LABELS,
    deliver_admin_reply,
    detect_kind,
    dump_payload,
    preview_of,
    sender_label,
    send_stored,
)
from ..utils import format_dt, now_iso, now_utc, parse_interval, to_iso

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Кнопки на уведомлениях об обращениях: доступны админам из ADMIN_IDS и участникам
# общего чата ADMIN_CHAT_ID. Подключается в main.py ДО router, иначе фильтр IsAdmin
# отсечёт участников чата и нажатие уйдёт в fallback («Кнопка устарела»).
notify_router = Router(name="admin_notifications")
notify_router.message.filter(IsAdminOrAdminChat())
notify_router.callback_query.filter(IsAdminOrAdminChat())

REPORTS_PAGE_SIZE = 5


class AdminFlow(StatesGroup):
    """Шаги админских сценариев."""

    waiting_reply = State()
    waiting_broadcast = State()
    broadcasting_mode = State()
    waiting_interval = State()


def _int_at(value: str | None, index: int, default: int = 0) -> int:
    try:
        return int((value or "").split(":")[index])
    except (IndexError, ValueError):
        return default


def _report_view_text(report, user, config: Config) -> str:
    return texts.REPORT_ADMIN_VIEW.format(
        report_id=report["id"],
        category=html.escape(texts.category_label(report["category"])),
        kind=KIND_LABELS.get(report["kind"], report["kind"]),
        sender=sender_label(report, user),
        created_at=format_dt(report["created_at"], config.display_utc_offset),
        status=texts.STATUS_LABELS.get(report["status"], report["status"]),
        preview=html.escape(report["preview"] or "—"),
    )


# --- меню и статистика ------------------------------------------------------


@router.callback_query(F.data == "adm:menu")
async def open_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(texts.ADMIN_MENU, reply_markup=keyboards.admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def show_stats(callback: CallbackQuery, db: Database, config: Config) -> None:
    stats = await db.stats()
    await callback.message.edit_text(
        texts.ADMIN_STATS.format(
            **stats, updated_at=format_dt(now_iso(), config.display_utc_offset)
        ),
        reply_markup=keyboards.back_to_menu(),
    )
    await callback.answer()


# --- обращения --------------------------------------------------------------


@router.callback_query(F.data.startswith("adm:reports:"))
async def show_reports(callback: CallbackQuery, db: Database, config: Config) -> None:
    page = max(0, _int_at(callback.data, 2))
    total = await db.count_reports()
    if total == 0:
        await callback.message.edit_text(texts.REPORTS_EMPTY, reply_markup=keyboards.back_to_menu())
        await callback.answer()
        return

    pages = max(1, (total + REPORTS_PAGE_SIZE - 1) // REPORTS_PAGE_SIZE)
    page = min(page, pages - 1)
    rows = await db.list_reports(limit=REPORTS_PAGE_SIZE, offset=page * REPORTS_PAGE_SIZE)
    await callback.message.edit_text(
        texts.REPORTS_HEADER.format(page=page + 1, pages=pages, total=total),
        reply_markup=keyboards.reports_list(
            [dict(row) for row in rows], page, pages, config.display_utc_offset
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:rep:"))
async def show_report(
    callback: CallbackQuery, db: Database, config: Config, bot: Bot
) -> None:
    report_id = _int_at(callback.data, 2)
    page = _int_at(callback.data, 3)
    report = await db.get_report(report_id)
    if report is None:
        await callback.answer(texts.REPORT_NOT_FOUND, show_alert=True)
        return

    user = await db.get_user(report["user_id"])
    await callback.message.answer(
        _report_view_text(report, user, config),
        reply_markup=keyboards.report_view(dict(report), page),
    )
    try:
        await send_stored(bot, report["payload"], callback.message.chat.id)
    except TelegramAPIError as exc:
        log.warning("Не удалось показать контент обращения #%s: %s", report_id, exc)
        await callback.message.answer(
            f"⚠️ Содержимое обращения #{report_id} не удалось отправить повторно."
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:content:"))
async def resend_content(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    report_id = _int_at(callback.data, 2)
    report = await db.get_report(report_id)
    if report is None:
        await callback.answer(texts.REPORT_NOT_FOUND, show_alert=True)
        return
    try:
        await send_stored(bot, report["payload"], callback.message.chat.id)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить контент обращения #%s: %s", report_id, exc)
        await callback.answer("Не удалось отправить содержимое.", show_alert=True)
        return
    await callback.answer()


@notify_router.callback_query(F.data.startswith("rep:done:"))
async def mark_done(callback: CallbackQuery, db: Database) -> None:
    report_id = _int_at(callback.data, 2)
    await db.set_report_status(report_id, "done", callback.from_user.id)
    await callback.answer(texts.REPORT_DONE.format(report_id=report_id), show_alert=True)


@notify_router.callback_query(F.data.startswith("rep:spam:"))
async def mark_spam(callback: CallbackQuery, db: Database) -> None:
    report_id = _int_at(callback.data, 2)
    await db.set_report_status(report_id, "spam", callback.from_user.id)
    await callback.answer(texts.REPORT_SPAM.format(report_id=report_id), show_alert=True)


@notify_router.callback_query(F.data.startswith("rep:reply:"))
async def start_reply(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    report_id = _int_at(callback.data, 2)
    report = await db.get_report(report_id)
    if report is None:
        await callback.answer(texts.REPORT_NOT_FOUND, show_alert=True)
        return

    await state.set_state(AdminFlow.waiting_reply)
    await state.update_data(report_id=report_id)
    await callback.message.answer(
        texts.REPLY_PROMPT.format(report_id=report_id),
        reply_markup=keyboards.cancel_only("adm:repcancel"),
    )
    await callback.answer()


@notify_router.callback_query(F.data == "adm:repcancel")
async def cancel_reply(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Ответ отменён.")
    await callback.answer()


@notify_router.message(StateFilter(AdminFlow.waiting_reply), ~F.text.startswith("/"))
async def send_reply(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    report_id = int((await state.get_data()).get("report_id", 0))
    report = await db.get_report(report_id)
    if report is None:
        await state.clear()
        await message.answer(texts.REPORT_NOT_FOUND)
        return

    kind = detect_kind(message)
    if kind is None:
        await message.answer(texts.UNSUPPORTED_TYPE)
        return

    try:
        await deliver_admin_reply(bot, report["user_id"], report_id, dump_payload(message))
    except TelegramAPIError as exc:
        log.warning("Ответ по обращению #%s не доставлен: %s", report_id, exc)
        await message.answer(texts.REPLY_FAILED)
    else:
        await db.set_report_status(report_id, "answered", message.from_user.id)
        await message.answer(texts.REPLY_SENT.format(report_id=report_id))
    await state.clear()


@router.callback_query(F.data == "adm:cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "❌ Действие отменено.", reply_markup=keyboards.back_to_menu()
    )


# --- экспорт ----------------------------------------------------------------


@router.callback_query(F.data == "adm:export")
async def export_reports(callback: CallbackQuery, db: Database, config: Config) -> None:
    rows = await db.all_reports_with_users()
    if not rows:
        await callback.answer(texts.EXPORT_EMPTY, show_alert=True)
        return

    config.export_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%d_%H%M%S")
    path = config.export_dir / f"reports_{stamp}.csv"

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            [
                "id",
                "created_at",
                "category",
                "kind",
                "anonymous",
                "user_id",
                "user_full_name",
                "user_username",
                "status",
                "preview",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    format_dt(row["created_at"], config.display_utc_offset),
                    texts.category_label(row["category"]),
                    KIND_LABELS.get(row["kind"], row["kind"]),
                    "да" if row["anonymous"] else "нет",
                    row["user_id"],
                    row["user_full_name"] or "",
                    row["user_username"] or "",
                    texts.STATUS_LABELS.get(row["status"], row["status"]),
                    (row["preview"] or "").replace("\n", " "),
                ]
            )

    await callback.message.answer_document(
        FSInputFile(path, filename=path.name),
        caption=texts.EXPORT_CAPTION.format(
            count=len(rows), created_at=format_dt(now_iso(), config.display_utc_offset)
        ),
    )
    await callback.answer()

    await callback.answer()
