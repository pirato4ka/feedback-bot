"""Приём сведений от информаторов."""

from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..config import Config
from ..db import Database
from ..sender import KIND_LABELS, deliver_report, detect_kind, dump_payload, preview_of
from ..utils import now_utc, parse_iso

router = Router(name="report")
# Обращения принимаются только в личке: в общих чатах бот молчит.
router.message.filter(F.chat.type == ChatType.PRIVATE)

DEFAULT_ANONYMOUS = True


class ReportFlow(StatesGroup):
    """Шаги сценария отправки обращения."""

    choosing_category = State()
    waiting_content = State()
    confirming = State()


def _card_text(data: dict) -> str:
    anonymous = data.get("anonymous", DEFAULT_ANONYMOUS)
    return texts.REPORT_CARD.format(
        category=html.escape(texts.category_label(data.get("category", ""))),
        kind=KIND_LABELS.get(data.get("kind", ""), "—"),
        mode=texts.MODE_ANONYMOUS if anonymous else texts.MODE_NAMED,
        preview=html.escape(data.get("preview") or "—"),
    )


async def start_report(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ReportFlow.choosing_category)
    await message.answer(texts.CHOOSE_CATEGORY, reply_markup=keyboards.categories())


async def start_urgent_report(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(category=texts.URGENT_CATEGORY, anonymous=DEFAULT_ANONYMOUS)
    await state.set_state(ReportFlow.waiting_content)
    await message.answer(texts.URGENT_ASK_CONTENT, reply_markup=keyboards.cancel_only())


@router.message(StateFilter(ReportFlow.choosing_category), ~F.text.startswith("/"))
async def remind_category(message: Message, state: FSMContext) -> None:
    """Пользователь написал текст вместо выбора темы — показываем кнопки снова."""
    await message.answer(texts.CHOOSE_CATEGORY, reply_markup=keyboards.categories())


@router.callback_query(F.data.startswith("report:cat:"))
async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in texts.CATEGORIES:
        await callback.answer("Неизвестная категория, выберите ещё раз.", show_alert=True)
        return

    await state.set_state(ReportFlow.waiting_content)
    await state.update_data(category=key, anonymous=DEFAULT_ANONYMOUS)
    await callback.message.edit_text(
        texts.ASK_CONTENT.format(category=html.escape(texts.category_label(key))),
        reply_markup=keyboards.cancel_only(),
    )
    await callback.answer()


@router.message(StateFilter(ReportFlow.waiting_content), ~F.text.startswith("/"))
async def receive_content(message: Message, state: FSMContext) -> None:
    kind = detect_kind(message)
    if kind is None:
        await message.answer(texts.UNSUPPORTED_TYPE)
        return

    data = await state.get_data()
    if not data.get("category"):
        await start_report(message, state)
        return

    await state.update_data(
        kind=kind,
        payload=dump_payload(message),
        preview=preview_of(message),
    )
    await state.set_state(ReportFlow.confirming)

    updated = await state.get_data()
    await message.answer(
        _card_text(updated),
        reply_markup=keyboards.report_card(updated.get("anonymous", DEFAULT_ANONYMOUS)),
    )


@router.callback_query(F.data == "report:anon")
async def toggle_anonymous(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("payload"):
        await callback.answer("Черновик устарел, начните заново.", show_alert=True)
        return

    anonymous = not data.get("anonymous", DEFAULT_ANONYMOUS)
    await state.update_data(anonymous=anonymous)
    data["anonymous"] = anonymous

    await callback.message.edit_text(
        _card_text(data), reply_markup=keyboards.report_card(anonymous)
    )
    await callback.answer("🕶 Имя скрыто" if anonymous else "👤 Имя будет показано")


@router.message(StateFilter(ReportFlow.confirming), ~F.text.startswith("/"))
async def remind_confirm(message: Message, state: FSMContext) -> None:
    """Вместо подтверждения пришло новое сообщение — показываем карточку ещё раз."""
    data = await state.get_data()
    if not data.get("payload"):
        await start_report(message, state)
        return
    await message.answer(
        _card_text(data),
        reply_markup=keyboards.report_card(data.get("anonymous", DEFAULT_ANONYMOUS)),
    )


@router.callback_query(F.data == "report:send")
async def send_report(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    config: Config,
    bot: Bot,
) -> None:
    data = await state.get_data()
    if not all(data.get(key) for key in ("category", "kind", "payload")):
        await state.clear()
        await callback.answer("Черновик устарел, начните заново: /start", show_alert=True)
        return

    cooldown = config.report_cooldown_seconds
    if cooldown:
        last = await db.last_report_at(callback.from_user.id)
        if last:
            left = cooldown - (now_utc() - parse_iso(last)).total_seconds()
            if left > 0:
                await callback.answer(
                    texts.COOLDOWN.format(seconds=int(left) + 1), show_alert=True
                )
                return

    report_id = await db.create_report(
        user_id=callback.from_user.id,
        category=data["category"],
        kind=data["kind"],
        preview=data.get("preview"),
        payload=data["payload"],
        anonymous=bool(data.get("anonymous", DEFAULT_ANONYMOUS)),
    )
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    report = await db.get_report(report_id)
    user = await db.get_user(callback.from_user.id)
    delivered = bool(report) and await deliver_report(bot, config, report, user)

    await callback.message.answer(
        texts.REPORT_SENT.format(report_id=report_id)
        if delivered
        else texts.REPORT_SEND_FAILED.format(report_id=report_id),
        reply_markup=keyboards.after_report(),
    )
    await callback.answer("✅ Отправлено" if delivered else "⚠️ Сохранено")


@router.callback_query(F.data == "report:cancel")
async def cancel_report(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(texts.REPORT_CANCELLED)
    await callback.answer()


@router.callback_query(F.data == "report:new")
async def new_report(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ReportFlow.choosing_category)
    await callback.message.answer(texts.CHOOSE_CATEGORY, reply_markup=keyboards.categories())
    await callback.answer()
