"""Определение и отправка содержимого сообщений любого поддерживаемого типа."""

from __future__ import annotations

import html
import json
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, Message

from . import keyboards, texts
from .config import Config
from .utils import format_dt, truncate

log = logging.getLogger(__name__)

KIND_LABELS: dict[str, str] = {
    "text": "текст",
    "photo": "фото",
    "video": "видео",
    "animation": "GIF-анимация",
    "audio": "аудио",
    "voice": "голосовое сообщение",
    "video_note": "видеосообщение (кружок)",
    "document": "документ",
    "sticker": "стикер",
    "location": "геолокация",
    "venue": "место",
    "contact": "контакт",
    "poll": "опрос",
    "dice": "кубик",
}

# Порядок важен: venue содержит location, поэтому проверяется раньше.
_DETECT_ORDER: tuple[str, ...] = (
    "text",
    "photo",
    "video",
    "animation",
    "audio",
    "voice",
    "video_note",
    "document",
    "sticker",
    "venue",
    "location",
    "contact",
    "poll",
    "dice",
)


def detect_kind(message: Message) -> str | None:
    """Возвращает тип содержимого сообщения, либо None если переслать его нельзя."""
    for field in _DETECT_ORDER:
        if getattr(message, field, None):
            return field
    return None


def preview_of(message: Message, limit: int = 300) -> str:
    """Короткое текстовое описание содержимого для карточек и списков."""
    text = message.text or message.caption
    if text:
        return truncate(text, limit)
    if message.poll is not None:
        return truncate(f"Опрос: {message.poll.question}", limit)
    if message.location is not None:
        return f"Геолокация: {message.location.latitude:.6f}, {message.location.longitude:.6f}"
    if message.venue is not None:
        return truncate(f"{message.venue.title} — {message.venue.address}", limit)
    if message.contact is not None:
        name = " ".join(filter(None, [message.contact.first_name, message.contact.last_name]))
        return truncate(f"Контакт: {name}", limit)
    if message.dice is not None:
        return f"Кубик: {message.dice.emoji} = {message.dice.value}"
    if message.sticker is not None:
        return truncate(f"Стикер {message.sticker.emoji or ''}".strip(), limit)
    if message.audio is not None:
        return truncate(f"Аудио: {message.audio.title or message.audio.file_name or ''}", limit)
    if message.document is not None:
        return truncate(f"Документ: {message.document.file_name or ''}", limit)
    if message.video_note is not None:
        return "Видеосообщение (кружок)"
    return KIND_LABELS.get(detect_kind(message) or "", "вложение")


def dump_payload(message: Message) -> str:
    """Сериализует сообщение в JSON для хранения и последующей отправки."""
    data: dict[str, Any] = message.model_dump(mode="json", exclude_none=True)
    # Кнопки и эффекты не должны уходить получателям вместе с копией.
    for key in ("reply_markup", "effect_id", "message_thread_id", "business_connection_id"):
        data.pop(key, None)
    return json.dumps(data, ensure_ascii=False)


def load_payload(payload: str, bot: Bot) -> Message:
    """Восстанавливает сообщение из JSON и привязывает к экземпляру бота."""
    return Message.model_validate(json.loads(payload)).as_(bot)


async def send_stored(
    bot: Bot,
    payload: str,
    chat_id: int,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Отправляет сохранённое содержимое в указанный чат."""
    message = load_payload(payload, bot)
    return await message.send_copy(chat_id, reply_markup=reply_markup)


def admin_targets(config: Config) -> list[int]:
    """Куда доставлять обращения: общий чат администрации либо личные чаты админов."""
    if config.admin_chat_id is not None:
        return [config.admin_chat_id]
    return sorted(config.admin_ids)


def build_report_header(report: Any, user: Any, config: Config) -> str:
    if report["anonymous"]:
        sender = texts.SENDER_ANONYMOUS
    else:
        username = ""
        name = "—"
        if user is not None:
            name = html.escape(user["full_name"] or "—")
            if user["username"]:
                username = f" (@{html.escape(user['username'])})"
        sender = texts.SENDER_NAMED.format(
            name=name, username=username, user_id=report["user_id"]
        )

    return texts.REPORT_ADMIN_HEADER.format(
        report_id=report["id"],
        category=html.escape(texts.category_label(report["category"])),
        kind=KIND_LABELS.get(report["kind"], report["kind"]),
        sender=sender,
        created_at=format_dt(report["created_at"], config.display_utc_offset),
    )


async def notify_admins(
    bot: Bot,
    config: Config,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Отправляет уведомление всем целям администрации, возвращает число успешных."""
    delivered = 0
    for chat_id in admin_targets(config):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
            delivered += 1
        except TelegramAPIError as exc:
            log.warning("Не удалось уведомить администратора %s: %s", chat_id, exc)
    return delivered


async def deliver_report(bot: Bot, config: Config, report: Any, user: Any) -> bool:
    """Доставляет обращение администрации: заголовок + копия содержимого."""
    header = build_report_header(report, user, config)
    markup = keyboards.report_actions(report["id"])
    delivered = False

    for chat_id in admin_targets(config):
        try:
            await bot.send_message(chat_id, header)
            delivered = True
        except TelegramAPIError as exc:
            log.warning("Заголовок обращения #%s не доставлен в %s: %s", report["id"], chat_id, exc)
            continue

        try:
            await send_stored(bot, report["payload"], chat_id, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("Содержимое обращения #%s не доставлено в %s: %s", report["id"], chat_id, exc)
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Контент обращения #{report['id']} не удалось переслать.\n"
                    "Откройте его в панели администрации: «📨 Обращения».",
                    reply_markup=markup,
                )
            except TelegramAPIError:
                pass

    return delivered


async def deliver_admin_reply(bot: Bot, user_id: int, report_id: int, payload: str) -> None:
    """Отправляет информатору ответ администрации без раскрытия личности админа."""
    await bot.send_message(user_id, texts.REPLY_HEADER.format(report_id=report_id))
    await send_stored(bot, payload, user_id)

