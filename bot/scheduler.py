"""Фоновый планировщик периодических рассылок."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

from . import sender, texts
from .config import Config
from .db import Database
from .utils import format_dt, humanize_interval, now_iso, now_utc, to_iso

log = logging.getLogger(__name__)


def _humanize_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} сек"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} сек" if sec else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


class BroadcastScheduler:
    """Раз в N секунд проверяет расписание и выполняет подошедшие рассылки.

    Рассылки выполняются последовательно: так мы не превышаем лимиты Telegram
    и не конкурируем за одно и то же содержимое.
    """

    def __init__(self, bot: Bot, db: Database, config: Config) -> None:
        self._bot = bot
        self._db = db
        self._config = config
        self._loop_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._running: set[int] = set()
        self._background: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="broadcast-scheduler")
        log.info("Планировщик рассылок запущен (тик %s c)", self._config.scheduler_tick_seconds)

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # планировщик не должен умирать из-за одной ошибки
                log.exception("Сбой в цикле планировщика рассылок")
            await asyncio.sleep(self._config.scheduler_tick_seconds)

    async def tick(self) -> None:
        for row in await self._db.due_broadcasts():
            self.schedule_now(row["id"])

    def schedule_now(self, broadcast_id: int) -> bool:
        """Запускает рассылку в фоне. False — если она уже выполняется."""
        if broadcast_id in self._running:
            return False
        self._running.add(broadcast_id)
        task = asyncio.create_task(
            self._execute(broadcast_id), name=f"broadcast-{broadcast_id}"
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return True

    async def _execute(self, broadcast_id: int) -> None:
        try:
            async with self._lock:
                row = await self._db.get_broadcast(broadcast_id)
                if row is None:
                    log.warning("Рассылка #%s не найдена", broadcast_id)
                    return
                await self._perform(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Рассылка #%s завершилась ошибкой", broadcast_id)
            await sender.notify_admins(
                self._bot,
                self._config,
                f"⚠️ Рассылку #{broadcast_id} не удалось выполнить. Подробности в логах.",
            )
        finally:
            self._running.discard(broadcast_id)

    async def _perform(self, row) -> None:
        broadcast_id = int(row["id"])
        started = now_utc()
        run_id = await self._db.start_broadcast_run(broadcast_id)
        recipients = await self._db.broadcast_recipients()
        sent = 0
        failed = 0

        try:
            message = sender.load_payload(row["payload"], self._bot)
        except Exception:
            log.exception("Не удалось восстановить содержимое рассылки #%s", broadcast_id)
            await self._db.finish_broadcast_run(run_id, 0, 0)
            await self._db.mark_broadcast_ran(
                broadcast_id,
                last_run_at=now_iso(),
                next_run_at=None,
                enabled=False,
            )
            await sender.notify_admins(
                self._bot,
                self._config,
                f"⚠️ Рассылка #{broadcast_id}: повреждено сохранённое содержимое, рассылка отключена.",
            )
            return

        log.info("Рассылка #%s: старт, получателей %s", broadcast_id, len(recipients))

        for user_id in recipients:
            try:
                await message.send_copy(user_id, reply_markup=None)
                sent += 1
            except TelegramRetryAfter as exc:
                log.warning("Лимит Telegram, пауза %s c", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await message.send_copy(user_id, reply_markup=None)
                    sent += 1
                except TelegramAPIError:
                    failed += 1
            except TelegramForbiddenError:
                await self._db.set_blocked(user_id, True)
                failed += 1
            except TelegramAPIError as exc:
                failed += 1
                log.warning("Ошибка отправки пользователю %s: %s", user_id, exc)

            await asyncio.sleep(1.0 / self._config.broadcast_rate_per_second)

        finished = now_utc()
        await self._db.finish_broadcast_run(run_id, sent, failed)

        next_run_at, enabled = self._next_schedule(row, finished)
        await self._db.mark_broadcast_ran(
            broadcast_id,
            last_run_at=to_iso(finished),
            next_run_at=next_run_at,
            enabled=enabled,
        )

        report = texts.BROADCAST_REPORT.format(
            broadcast_id=broadcast_id,
            sent=sent,
            failed=failed,
            total=len(recipients),
            duration=_humanize_duration((finished - started).total_seconds()),
            finished_at=format_dt(finished, self._config.display_utc_offset),
        )
        if next_run_at:
            report += texts.BROADCAST_REPORT_NEXT.format(
                next_run=format_dt(next_run_at, self._config.display_utc_offset)
            )
        await sender.notify_admins(self._bot, self._config, report)
        log.info(
            "Рассылка #%s завершена: %s/%s, ошибок %s",
            broadcast_id,
            sent,
            len(recipients),
            failed,
        )

    @staticmethod
    def _next_schedule(row, finished) -> tuple[str | None, bool]:
        if row["kind"] == texts.BROADCAST_MODE_PERIODIC and row["interval_seconds"]:
            interval = int(row["interval_seconds"])
            # Пауза не снимается автоматически: ручной запуск не включает рассылку.
            return to_iso(finished + timedelta(seconds=interval)), bool(row["enabled"])
        return None, False

    @staticmethod
    def describe_schedule(row) -> str:
        if row["kind"] == texts.BROADCAST_MODE_PERIODIC and row["interval_seconds"]:
            return texts.SCHEDULE_PERIODIC.format(
                interval=humanize_interval(int(row["interval_seconds"]))
            )
        return texts.SCHEDULE_ONCE

