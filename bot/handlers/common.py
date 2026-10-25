"""Общие команды, приветствие и главное меню."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import keyboards, texts
from ..config import Config
from .report import start_report, start_urgent_report

router = Router(name="common")
# Сценарии информатора работают только в личке, чтобы бот не отвечал в общих чатах.
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    name = html.escape(message.from_user.full_name or "друг")
    await message.answer(
        texts.WELCOME.format(name=name),
        reply_markup=keyboards.main_menu(config.is_admin(message.from_user.id)),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer(
        "🏠 Главное меню.",
        reply_markup=keyboards.main_menu(config.is_admin(message.from_user.id)),
    )


@router.message(Command("help"))
@router.message(F.text == keyboards.BTN_HELP)
async def cmd_help(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer(
        texts.HELP,
        reply_markup=keyboards.main_menu(config.is_admin(message.from_user.id)),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, config: Config) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    await message.answer(
        texts.REPORT_CANCELLED if had_state else "ℹ️ Сейчас нечего отменять.",
        reply_markup=keyboards.main_menu(config.is_admin(message.from_user.id)),
    )


@router.message(F.text == keyboards.BTN_REPORT)
async def on_report_button(message: Message, state: FSMContext) -> None:
    await start_report(message, state)


@router.message(F.text == keyboards.BTN_URGENT)
async def on_urgent_button(message: Message, state: FSMContext) -> None:
    await start_urgent_report(message, state)


@router.message(Command("admin"))
@router.message(F.text == keyboards.BTN_ADMIN)
async def cmd_admin(message: Message, state: FSMContext, config: Config) -> None:
    if not config.is_admin(message.from_user.id):
        await message.answer(texts.ADMIN_DENIED)
        return
    await state.clear()
    await message.answer(texts.ADMIN_MENU, reply_markup=keyboards.admin_menu())
