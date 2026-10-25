"""Точка входа: python run.py"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)

from bot.config import Config, ConfigError
from bot.main import run_bot, setup_logging

log = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    try:
        config = Config.load()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    except ValueError as exc:
        log.error("Некорректное значение в .env: %s", exc)
        return 1

    try:
        asyncio.run(run_bot(config))
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил BOT_TOKEN. Проверьте токен из @BotFather.")
        return 1
    except TelegramNetworkError as exc:
        log.error("Нет связи с Telegram API: %s", exc)
        return 1
    except TelegramAPIError as exc:
        log.error("Ошибка Telegram API при запуске: %s", exc)
        return 1
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка по сигналу")
    return 0


if __name__ == "__main__":
    sys.exit(main())
