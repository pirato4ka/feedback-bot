"""Сборка и запуск бота."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from . import __version__
from .config import Config
from .db import Database
from .handlers import admin, broadcasts, common, fallback, report
from .middlewares import UserMiddleware
from .scheduler import BroadcastScheduler

log = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def build_dispatcher(
    config: Config, db: Database, scheduler: BroadcastScheduler
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["config"] = config
    dp["db"] = db
    dp["scheduler"] = scheduler

    user_middleware = UserMiddleware()
    dp.message.middleware(user_middleware)
    dp.callback_query.middleware(user_middleware)

    # Порядок важен: админские сценарии раньше общих, fallback — последним.
    dp.include_routers(
        admin.router,
        broadcasts.router,
        report.router,
        common.router,
        fallback.router,
    )

    @dp.errors()
    async def handle_error(event: ErrorEvent) -> bool:
        log.error(
            "Ошибка при обработке обновления %s",
            event.update.update_id,
            exc_info=event.exception,
        )
        return True

    return dp


async def run_bot(config: Config) -> None:
    bot = Bot(
        config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    db = Database(config.db_path)
    await db.connect()
    scheduler = BroadcastScheduler(bot, db, config)

    try:
        dispatcher = build_dispatcher(config, db, scheduler)
        await scheduler.start()
        me = await bot.get_me()
        log.info("Бот @%s запущен (версия %s)", me.username, __version__)
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        await scheduler.stop()
        await bot.session.close()
        await db.close()
        log.info("Бот остановлен")
