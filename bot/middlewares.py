"""Middleware и фильтры."""

from __future__ import annotations

from typing import Any, Callable

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import Config
from .db import Database


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя и отсекает заблокировавших бота."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Any],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)

        db: Database = data["db"]
        await db.upsert_user(user.id, user.username, user.full_name, user.language_code)

        if await db.is_blocked(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None

        return await handler(event, data)


class IsAdmin(BaseFilter):
    """Пропускает только администраторов из ADMIN_IDS."""

    async def __call__(
        self, event: Message | CallbackQuery, config: Config
    ) -> bool:
        user = event.from_user
        return bool(user and config.is_admin(user.id))


class IsAdminOrAdminChat(BaseFilter):
    """Администратор из ADMIN_IDS либо участник общего чата администрации.

    Нужен для кнопок на уведомлениях об обращениях: их нажимают в чате
    ADMIN_CHAT_ID, где участники не обязаны быть перечислены в ADMIN_IDS.
    """

    async def __call__(
        self, event: Message | CallbackQuery, config: Config
    ) -> bool:
        user = event.from_user
        if config.is_admin(user.id if user else None):
            return True
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = message.chat if message is not None else None
        return bool(config.admin_chat_id and chat and chat.id == config.admin_chat_id)
