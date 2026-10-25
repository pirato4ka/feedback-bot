"""Админ-панель: управление рассылками."""

from __future__ import annotations

import html
import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..config import Config
from ..db import Database
from ..middlewares import IsAdmin
from ..scheduler import BroadcastScheduler
from ..sender import KIND_LABELS, detect_kind, dump_payload, preview_of, send_stored
from ..utils import format_dt, now_iso, now_utc, parse_interval, to_iso
from .admin import AdminFlow

log = logging.getLogger(__name__)

router = Router(name="broadcasts")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _int_at(value: str | None, index: int) -> int:
    try:
        return int((value or "").split(":")[index])
    except (IndexError, ValueError):
        return 0


async def _view_text(row, db: Database, config: Config) -> str:
    sent_total, failed_total = await db.broadcast_totals(row["id"])
    last = await db.last_broadcast_run(row["id"])
    last_run = "—"
    if last is not None:
        moment = last["finished_at"] or last["started_at"]
        last_run = (
            f"{format_dt(moment, config.display_utc_offset)} · "
            f"✅ {last['sent']} / ⚠️ {last['failed']}"
        )

    return texts.BROADCAST_VIEW.format(
        broadcast_id=row["id"],
        schedule=texts.schedule_label(row["kind"], row["interval_seconds"]),
        status="▶️ включена" if row["enabled"] else "⏸ на паузе",
        next_run=format_dt(row["next_run_at"], config.display_utc_offset),
        last_run=last_run,
        created_at=format_dt(row["created_at"], config.display_utc_offset),
        created_by=row["created_by"],
        sent_total=sent_total,
        failed_total=failed_total,
        preview=html.escape(row["preview"] or "—"),
    )


async def _render_list(message: Message, db: Database) -> None:
    rows = [dict(row) for row in await db.list_broadcasts()]
    text = texts.BROADCASTS_HEADER if rows else texts.BROADCASTS_EMPTY
    await message.edit_text(text, reply_markup=keyboards.broadcasts_list(rows))


# --- список и создание ------------------------------------------------------


@router.callback_query(F.data == "adm:bc")
async def open_broadcasts(callback: CallbackQuery, db: Database) -> None:
    await _render_list(callback.message, db)
    await callback.answer()


@router.callback_query(F.data == "adm:bc:new")
async def new_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_broadcast)
    await state.set_data({})
    await callback.message.answer(
        texts.BROADCAST_ASK_CONTENT, reply_markup=keyboards.cancel_only("adm:bc:cancel")
    )
    await callback.answer()


@router.message(StateFilter(AdminFlow.waiting_broadcast), ~F.text.startswith("/"))
async def broadcast_content(message: Message, state: FSMContext, db: Database) -> None:
    kind = detect_kind(message)
    if kind is None:
        await message.answer(texts.UNSUPPORTED_TYPE)
        return

    preview = preview_of(message, 150)
    recipients = await db.count_users(only_active=True)
    await state.update_data(
        payload=dump_payload(message), content_kind=kind, preview=preview
    )
    await state.set_state(AdminFlow.broadcasting_mode)
    await message.answer(
        texts.BROADCAST_CARD.format(
            kind=KIND_LABELS.get(kind, kind),
            preview=html.escape(preview),
            recipients=recipients,
        ),
        reply_markup=keyboards.broadcast_mode(),
    )


@router.callback_query(F.data == "adm:bc:cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "❌ Создание рассылки отменено.", reply_markup=keyboards.back_to_menu("adm:bc")
    )
    await callback.answer()


@router.callback_query(
    F.data == "adm:bc:mode:periodic", StateFilter(AdminFlow.broadcasting_mode)
)
async def choose_periodic(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.waiting_interval)
    await callback.message.edit_text(
        texts.BROADCAST_CHOOSE_INTERVAL, reply_markup=keyboards.broadcast_intervals()
    )
    await callback.answer()



async def _show_confirm(
    event: CallbackQuery | Message,
    state: FSMContext,
    db: Database,
    config: Config,
    seconds: int,
) -> None:
    data = await state.get_data()
    recipients = await db.count_users(only_active=True)
    first_run_at = to_iso(now_utc() + timedelta(seconds=seconds))
    await state.update_data(interval_seconds=seconds)
    await state.set_state(AdminFlow.broadcasting_mode)

    text = texts.BROADCAST_CONFIRM.format(
        schedule=texts.schedule_label(texts.BROADCAST_MODE_PERIODIC, seconds),
        first_run=format_dt(first_run_at, config.display_utc_offset),
        recipients=recipients,
        preview=html.escape(data.get("preview") or "—"),
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboards.broadcast_confirm())
    else:
        await event.answer(text, reply_markup=keyboards.broadcast_confirm())


@router.callback_query(
    F.data.startswith("adm:bc:int:"), StateFilter(AdminFlow.waiting_interval)
)
async def choose_interval(
    callback: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    raw = (callback.data or "").split(":")[3]
    if raw == "custom":
        await callback.message.edit_text(
            texts.BROADCAST_CHOOSE_INTERVAL
            + "\n\n✍️ Отправьте интервал сообщением: <code>90</code>, <code>2h</code> "
            "или <code>1d</code>.",
            reply_markup=keyboards.cancel_only("adm:bc:cancel"),
        )
        await callback.answer()
        return

    try:
        seconds = int(raw)
    except ValueError:
        await callback.answer("Не удалось разобрать интервал.", show_alert=True)
        return

    await _show_confirm(callback, state, db, config, seconds)
    await callback.answer()


@router.message(StateFilter(AdminFlow.waiting_interval), ~F.text.startswith("/"))
async def custom_interval(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    seconds, error = parse_interval(message.text or "")
    if seconds is None:
        await message.answer(error or "Не удалось разобрать интервал.")
        return

    await _show_confirm(message, state, db, config, seconds)


@router.callback_query(
    F.data == "adm:bc:mode:once", StateFilter(AdminFlow.broadcasting_mode)
)
async def create_once(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    scheduler: BroadcastScheduler,
) -> None:
    await _create(callback, state, db, scheduler, run_now=True)


@router.callback_query(
    F.data.startswith("adm:bc:make:"), StateFilter(AdminFlow.broadcasting_mode)
)
async def create_scheduled(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    config: Config,
    scheduler: BroadcastScheduler,
) -> None:
    run_now = (callback.data or "").endswith("now")
    await _create(callback, state, db, scheduler, run_now=run_now, config=config)


async def _create(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    scheduler: BroadcastScheduler,
    *,
    run_now: bool,
    config: Config | None = None,
) -> None:
    data = await state.get_data()
    payload = data.get("payload")
    if not payload:
        await state.clear()
        await callback.answer("Черновик устарел, создайте рассылку заново.", show_alert=True)
        return

    interval = data.get("interval_seconds")
    periodic = bool(interval)
    if periodic and not run_now:
        first_run_at = to_iso(now_utc() + timedelta(seconds=int(interval)))
    else:
        first_run_at = now_iso()

    broadcast_id = await db.create_broadcast(
        payload=payload,
        preview=data.get("preview"),
        kind=texts.BROADCAST_MODE_PERIODIC if periodic else texts.BROADCAST_MODE_ONCE,
        interval_seconds=int(interval) if periodic else None,
        next_run_at=first_run_at,
        enabled=True,
        created_by=callback.from_user.id,
    )
    await state.clear()

    if run_now:
        scheduler.schedule_now(broadcast_id)
        text = texts.BROADCAST_CREATED_ONCE.format(broadcast_id=broadcast_id)
    else:
        offset = config.display_utc_offset if config else 0
        text = texts.BROADCAST_CREATED_PERIODIC.format(
            broadcast_id=broadcast_id,
            schedule=texts.schedule_label(texts.BROADCAST_MODE_PERIODIC, int(interval or 0)),
            first_run=format_dt(first_run_at, offset),
        )

    await callback.message.edit_text(text, reply_markup=keyboards.back_to_menu("adm:bc"))
    await callback.answer("✅ Готово")



# --- карточка рассылки ------------------------------------------------------


@router.callback_query(F.data.startswith("adm:bc:view:"))
async def view_broadcast(callback: CallbackQuery, db: Database, config: Config) -> None:
    broadcast_id = _int_at(callback.data, 3)
    row = await db.get_broadcast(broadcast_id)
    if row is None:
        await callback.answer(texts.BROADCAST_NOT_FOUND, show_alert=True)
        return

    await callback.message.edit_text(
        await _view_text(row, db, config),
        reply_markup=keyboards.broadcast_view(dict(row)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:bc:show:"))
async def show_content(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    broadcast_id = _int_at(callback.data, 3)
    row = await db.get_broadcast(broadcast_id)
    if row is None:
        await callback.answer(texts.BROADCAST_NOT_FOUND, show_alert=True)
        return
    try:
        await send_stored(bot, row["payload"], callback.message.chat.id)
    except TelegramAPIError as exc:
        log.warning("Не удалось показать контент рассылки #%s: %s", broadcast_id, exc)
        await callback.answer(texts.BROADCAST_CONTENT_EMPTY, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("adm:bc:toggle:"))
async def toggle_broadcast(
    callback: CallbackQuery, db: Database, config: Config
) -> None:
    broadcast_id = _int_at(callback.data, 3)
    row = await db.get_broadcast(broadcast_id)
    if row is None:
        await callback.answer(texts.BROADCAST_NOT_FOUND, show_alert=True)
        return

    enabled = not bool(row["enabled"])
    await db.set_broadcast_enabled(broadcast_id, enabled)
    row = await db.get_broadcast(broadcast_id)
    if row is None:
        await callback.answer(texts.BROADCAST_NOT_FOUND, show_alert=True)
        return

    await callback.message.edit_text(
        await _view_text(row, db, config),
        reply_markup=keyboards.broadcast_view(dict(row)),
    )
    await callback.answer(
        texts.BROADCAST_TOGGLED_ON.format(broadcast_id=broadcast_id)
        if enabled
        else texts.BROADCAST_TOGGLED_OFF.format(broadcast_id=broadcast_id)
    )


@router.callback_query(F.data.startswith("adm:bc:run:"))
async def run_broadcast(
    callback: CallbackQuery, db: Database, scheduler: BroadcastScheduler
) -> None:
    broadcast_id = _int_at(callback.data, 3)
    row = await db.get_broadcast(broadcast_id)
    if row is None:
        await callback.answer(texts.BROADCAST_NOT_FOUND, show_alert=True)
        return
    if not scheduler.schedule_now(broadcast_id):
        await callback.answer(texts.BROADCAST_ALREADY_RUNNING, show_alert=True)
        return
    await callback.answer(
        texts.BROADCAST_RUN_STARTED.format(broadcast_id=broadcast_id), show_alert=True
    )


@router.callback_query(F.data.startswith("adm:bc:del:"))
async def confirm_delete(callback: CallbackQuery, db: Database) -> None:
    broadcast_id = _int_at(callback.data, 3)
    row = await db.get_broadcast(broadcast_id)
    if row is None:
        await callback.answer(texts.BROADCAST_NOT_FOUND, show_alert=True)
        return

    await callback.message.edit_text(
        texts.BROADCAST_DELETE_CONFIRM.format(
            broadcast_id=broadcast_id,
            schedule=texts.schedule_label(row["kind"], row["interval_seconds"]),
        ),
        reply_markup=keyboards.broadcast_delete_confirm(broadcast_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:bc:delok:"))
async def delete_broadcast(callback: CallbackQuery, db: Database) -> None:
    broadcast_id = _int_at(callback.data, 3)
    await db.delete_broadcast(broadcast_id)
    rows = [dict(row) for row in await db.list_broadcasts()]
    text = texts.BROADCAST_DELETED.format(broadcast_id=broadcast_id)
    text += "\n\n" + (texts.BROADCASTS_HEADER if rows else texts.BROADCASTS_EMPTY)
    await callback.message.edit_text(text, reply_markup=keyboards.broadcasts_list(rows))
    await callback.answer()
