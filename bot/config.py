"""Конфигурация бота: читается из переменных окружения / файла .env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Некорректная или неполная конфигурация."""


def _parse_ints(raw: str) -> frozenset[int]:
    return frozenset(int(item) for item in re.split(r"[,\s]+", raw.strip()) if item)


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    return int(raw.strip())


@dataclass(frozen=True, slots=True)
class Config:
    """Настройки приложения."""

    bot_token: str
    admin_ids: frozenset[int]
    admin_chat_id: int | None
    db_path: Path
    export_dir: Path
    report_cooldown_seconds: int = 60
    scheduler_tick_seconds: int = 20
    broadcast_rate_per_second: float = 20.0
    display_utc_offset: int = 3

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or BASE_DIR / ".env")

        token = (os.getenv("BOT_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен из @BotFather."
            )
        if not re.fullmatch(r"\d+:[\w-]+", token):
            raise ConfigError("BOT_TOKEN имеет неверный формат. Ожидается вид 123456789:AA...")

        admin_ids = _parse_ints(os.getenv("ADMIN_IDS") or "")
        if not admin_ids:
            raise ConfigError(
                "Не задан ADMIN_IDS. Укажите Telegram ID администраторов через запятую "
                "(узнать свой ID можно у @userinfobot)."
            )

        db_path = Path(os.getenv("DB_PATH") or BASE_DIR / "data" / "bot.db")
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path

        export_dir = db_path.parent / "exports"

        return cls(
            bot_token=token,
            admin_ids=admin_ids,
            admin_chat_id=_parse_optional_int(os.getenv("ADMIN_CHAT_ID")),
            db_path=db_path,
            export_dir=export_dir,
            report_cooldown_seconds=max(0, int(os.getenv("REPORT_COOLDOWN_SECONDS") or 60)),
            scheduler_tick_seconds=max(5, int(os.getenv("SCHEDULER_TICK_SECONDS") or 20)),
            broadcast_rate_per_second=max(
                1.0, float(os.getenv("BROADCAST_RATE_PER_SECOND") or 20)
            ),
            display_utc_offset=int(os.getenv("DISPLAY_UTC_OFFSET") or 3),
        )
