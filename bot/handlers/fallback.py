"""Ответы на неожиданные сообщения и устаревшие кнопки."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..config import Config

router = Router(name="fallback")


@router.message()
async def fallback_message(message: Message, config: Config) -> None:
    await message.answer(
        texts.FALLBACK,
        reply_markup=keyboards.main_menu(config.is_admin(message.from_user.id)),
    )


@router.callback_query()
async def fallback_callback(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела. Откройте меню заново: /start")
