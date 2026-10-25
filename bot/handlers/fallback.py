"""Ответы на неожиданные сообщения и устаревшие кнопки."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..config import Config

log = logging.getLogger(__name__)

router = Router(name="fallback")


@router.message()
async def fallback_message(message: Message, config: Config) -> None:
    await message.answer(
        texts.FALLBACK,
        reply_markup=keyboards.main_menu(config.is_admin(message.from_user.id)),
    )


@router.callback_query()
async def fallback_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if data.startswith("rep:"):
        user_id = callback.from_user.id
        log.info("Отказ в действии по обращению: user_id=%s data=%s", user_id, data)
        await callback.answer(
            "⛔ Действие доступно только администрации общины.\n"
            f"Ваш id: {user_id}",
            show_alert=True,
        )
        return
    await callback.answer("Кнопка устарела. Откройте меню заново: /start")
