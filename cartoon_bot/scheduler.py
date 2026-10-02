"""Планировщик напоминаний.

Каждое напоминание — строчка в таблице jobs: кому, что и когда отправить.
Раз в несколько секунд бот смотрит, не пора ли что-то отправить.
Поэтому при перезапуске бота напоминания не теряются.
"""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

import db
import funnel
import history
import keyboards as kb
import texts
from config import settings
from utils import esc, fmt, format_timer, full_price_text, is_quiet, shift_quiet

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Нет связи с Telegram или он сам сбоит: такое напоминание повторяем, пока связь не вернётся,
# и попыткой это не считаем (иначе получасовой сбой стёр бы все напоминания этого времени)
RETRY_OFFLINE_SECONDS = 300


def _is_offline(exc: Exception) -> bool:
    """Сбой связи, ошибка сервера Telegram или «подожди» — пройдёт само. Слишком большой файл — не пройдёт."""
    if isinstance(exc, TelegramEntityTooLarge):
        return False
    return isinstance(exc, (TelegramNetworkError, TelegramServerError, TelegramRetryAfter))


def _sales_allowed(user: dict) -> bool:
    """Слать ли «скоро конец скидки» / «скидка закончилась» этому ученику."""
    return settings.sales_reminders_for_all or bool(user["first_offer_view_at"])


def _skip_admin(user_id: int) -> bool:
    """Админ не получает напоминаний, кроме тестового режима (когда проверяет воронку)."""
    return user_id == settings.admin_id and not settings.test_mode


async def process_job(bot: Bot, job: dict) -> None:
    user = await db.get_user(job["user_id"])
    if user is None or user["blocked"] or user["paid_at"] or _skip_admin(job["user_id"]):
        await db.delete_job(job["id"])
        return

    ts = db.now()
    if is_quiet(ts):
        # бот мог быть выключен, и напоминание «проспало» до ночи — переносим на утро (это не «попытка»)
        await db.reschedule_job(job["id"], shift_quiet(ts), job["attempts"])
        return

    user_id = user["user_id"]
    name = esc(user["first_name"] or "")
    kind = job["kind"]

    if kind in funnel.STEP_JOBS:
        step = job["step"]
        if user["current_step"] == step and not user["finished_at"]:
            template = texts.REMIND_STEP_24 if kind == "step_24" else texts.REMIND_STEP_72
            sent = await funnel.send_text(
                bot,
                user_id,
                fmt(template, name=name, step=step),
                kb.remind_step_kb(step, with_help=(kind == "step_24")),
            )
            if sent is not None:
                await history.track(user_id, "reminder", step, kind)

    elif kind == "offer_24":
        if user["offer_shown_at"]:
            template = texts.OFFER_REMIND_24 if user["finished_at"] else texts.OFFER_REMIND_24_NOT_FINISHED
            text = fmt(template, name=name)
            left = funnel.discount_left(user, ts)
            if left > 0:
                text += "\n\n" + fmt(texts.OFFER_REMIND_24_TIMER, timer=format_timer(left))
            if await funnel.send_text(bot, user_id, text, kb.offer_reminder_kb(discount_active=left > 0)) is not None:
                await history.track(user_id, "reminder", detail=kind)

    elif kind == "offer_last_call":
        left = funnel.discount_left(user, ts)
        if left >= funnel.min_last_call_left() and _sales_allowed(user):
            sent = await funnel.send_text(
                bot,
                user_id,
                fmt(texts.OFFER_LAST_CALL, timer=format_timer(left), full_price=full_price_text()),
                kb.offer_reminder_kb(with_author=True, discount_active=True),
            )
            if sent is not None:
                await history.track(user_id, "reminder", detail=kind)

    elif kind == "inactive_offer":
        # повтор оффера после «Давно не виделись», если с первого раза он не отправился
        if not user["offer_shown_at"]:
            await funnel.show_offer(bot, user_id, funnel=True, as_screen=False)

    elif kind == "offer_ended":
        if funnel.discount_left(user, ts) == 0 and _sales_allowed(user):
            sent = await funnel.send_text(
                bot, user_id, fmt(texts.OFFER_ENDED, full_price=full_price_text()), kb.offer_reminder_kb(discount_active=False)
            )
            if sent is not None:
                await history.track(user_id, "reminder", detail=kind)

    else:
        logger.warning("Неизвестный тип напоминания %r", kind)

    await db.delete_job(job["id"])


async def process_due_jobs(bot: Bot) -> None:
    for job in await db.due_jobs(db.now()):
        attempts = job["attempts"] + 1
        if attempts > MAX_ATTEMPTS:
            logger.error("Напоминание %s (%s) так и не отправилось — удаляю", job["id"], job["kind"])
            await db.delete_job(job["id"])
            continue
        # Сначала «занимаем» напоминание (переносим на потом). Если база не пишется —
        # ничего не отправляем, а при сбое отправки оно само повторится позже, но не больше MAX_ATTEMPTS раз.
        await db.reschedule_job(job["id"], db.now() + 60 * attempts, attempts)
        try:
            await process_job(bot, job)  # при успехе удаляет напоминание
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой связи и т.п.: попробуем позже
            if _is_offline(exc):
                await db.reschedule_job(job["id"], db.now() + RETRY_OFFLINE_SECONDS, job["attempts"])
                logger.warning("Нет связи с Telegram (%s) — напоминание %s повторим через 5 минут", exc, job["id"])
                break  # остальные напоминания попробуем на следующей проверке
            logger.warning("Напоминание %s не отправилось (%s) — повторим позже", job["id"], exc)


# ID ученика → сколько раз подряд не получилось отправить «Давно не виделись»
_inactive_failures: dict[int, int] = {}


async def process_inactivity(bot: Bot) -> None:
    """«Давно не виделись» + оффер тем, кто пропал на 7 дней и ещё не видел оффер."""
    ts = db.now()
    if is_quiet(ts):
        return
    inactivity = settings.hours(settings.inactivity_hours)
    for user in await db.inactive_candidates(ts - inactivity):
        user_id = user["user_id"]
        if _skip_admin(user_id) or ts < shift_quiet(user["last_activity_at"] + inactivity):
            continue
        template = texts.INACTIVE_7_DAYS if funnel.discount_active(user, ts) else texts.INACTIVE_7_DAYS_NO_DISCOUNT
        # сначала отмечаем — если база не пишется, ничего не отправляем (и не шлём одно и то же по кругу)
        await db.update_user(user_id, inactive_sent=1)
        try:
            sent = await funnel.send_text(bot, user_id, fmt(template, name=esc(user["first_name"] or "")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — попробуем на следующей проверке, но не бесконечно
            if _is_offline(exc):
                # нет связи или лимит Telegram — это не попытка; этого и остальных попробуем на следующей проверке
                await db.update_user(user_id, inactive_sent=0)
                logger.warning("«Давно не виделись» для %s не отправилось (%s) — повторим позже", user_id, exc)
                break
            failures = _inactive_failures[user_id] = _inactive_failures.get(user_id, 0) + 1
            if failures < MAX_ATTEMPTS:
                await db.update_user(user_id, inactive_sent=0)
            else:
                _inactive_failures.pop(user_id, None)
            logger.warning("«Давно не виделись» для %s не отправилось (%s) — попытка %s", user_id, exc, failures)
            continue
        _inactive_failures.pop(user_id, None)
        if sent is not None:
            await history.track(user_id, "reminder", detail="inactive")
            try:
                await funnel.show_offer(bot, user_id, funnel=True, as_screen=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — оффер повторим через планировщик
                logger.warning("Оффер после «Давно не виделись» для %s не отправился (%s) — повторим", user_id, exc)
                await db.add_job(user_id, "inactive_offer", db.now() + 60)
        await asyncio.sleep(0.05)


async def run(bot: Bot) -> None:
    logger.info(
        "Планировщик запущен (проверка каждые %s сек, тестовый режим: %s)",
        settings.scheduler_tick,
        texts.YES if settings.test_mode else texts.NO,
    )
    while True:
        try:
            await process_due_jobs(bot)
            await process_inactivity(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка в планировщике")
        await asyncio.sleep(settings.scheduler_tick)
