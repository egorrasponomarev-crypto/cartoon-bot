"""Точка запуска бота: python bot.py"""
import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

import db
import funnel
import keyboards
import scheduler
import texts
from config import settings
from handlers import admin, offer, payments, start, steps, support
from middlewares import ActivityMiddleware
from utils import check_texts, esc, fmt

logger = logging.getLogger("bot")


async def set_commands(bot: Bot) -> None:
    """Подсказки команд в меню Telegram (кнопка «/» или «Меню» слева от поля ввода)."""
    user_commands = [BotCommand(command="start", description=texts.CMD_START)]
    if settings.payment_mode != "preorder":
        # поддержка по оплате и условия покупки нужны, только когда оплата идёт в самом боте
        user_commands += [
            BotCommand(command="paysupport", description=texts.CMD_PAYSUPPORT),
            BotCommand(command="terms", description=texts.CMD_TERMS),
        ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    if not settings.admin_id:
        return
    admin_commands = user_commands + [
        BotCommand(command="admin", description=texts.CMD_ADMIN),
        BotCommand(command="stats", description=texts.CMD_STATS),
        BotCommand(command="broadcast", description=texts.CMD_BROADCAST),
        BotCommand(command="fileid", description=texts.CMD_FILEID),
        BotCommand(command="reset", description=texts.CMD_RESET),
        BotCommand(command="refund", description=texts.CMD_REFUND),
        BotCommand(command="grant", description=texts.CMD_GRANT),
    ]
    try:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=settings.admin_id))
    except TelegramAPIError as exc:
        logger.warning("Не получилось настроить команды админа (%s). Нажми /start в боте со своего аккаунта.", exc)


async def resolve_help_url(bot: Bot) -> None:
    """Кнопка «Нужна помощь» ведёт в чат с админом: берём его username у Telegram (если HELP_URL не задан)."""
    if settings.help_url:
        keyboards.set_help_url(settings.help_url)
        logger.info("Кнопка «Нужна помощь» ведёт на %s (HELP_URL)", settings.help_url)
        return
    if not settings.admin_id:
        return
    try:
        chat = await bot.get_chat(settings.admin_id)
        username = getattr(chat, "username", None)
    except Exception as exc:  # noqa: BLE001 — без ссылки кнопка просто откроет вопрос внутри бота
        logger.warning("Не удалось узнать username админа (%s) — «Нужна помощь» откроет вопрос внутри бота", exc)
        return
    if isinstance(username, str) and username:
        keyboards.set_help_url(f"https://t.me/{username}")
        logger.info("Кнопка «Нужна помощь» ведёт в чат с @%s", username)
    else:
        logger.warning("У админа нет username — «Нужна помощь» откроет вопрос внутри бота (или задай HELP_URL в .env)")


def build_dispatcher() -> Dispatcher:
    # SimpleEventIsolation: нажатия одного ученика обрабатываются по очереди,
    # поэтому двойное нажатие не создаёт дублей напоминаний и заявок
    # (а повтор той же кнопки в течение 3 секунд отсекает ActivityMiddleware)
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.message.outer_middleware(ActivityMiddleware())
    dp.callback_query.outer_middleware(ActivityMiddleware())
    # Порядок важен: сначала оплата (её нельзя пропустить), потом админ, меню и шаги,
    # в самом конце — «всё остальное» (support)
    dp.include_routers(payments.router, admin.router, start.router, steps.router, offer.router, support.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.init_db(settings.db_path)
    session = AiohttpSession(proxy=settings.proxy) if settings.proxy else AiohttpSession()
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Базу закрываем при любом исходе: открытая база не даёт процессу завершиться, и если при запуске
    # нет связи с Telegram, бот «висел» бы вместо того, чтобы упасть и перезапуститься (systemd Restart=always)
    try:
        await run(bot)
    finally:
        await db.close_db()
        await bot.session.close()


async def run(bot: Bot) -> None:
    dp = build_dispatcher()

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram не принял BOT_TOKEN. Проверь токен в файле .env (его выдаёт @BotFather).")

    mode_label = texts.YES if settings.test_mode else texts.NO
    logger.info("Бот @%s запущен. Оплата: %s. Тестовый режим: %s", me.username, settings.payment_mode, mode_label)
    if not settings.admin_id:
        logger.warning("ADMIN_ID не указан в .env — функции админа отключены. Узнать свой ID: команда /myid")

    await set_commands(bot)
    await resolve_help_url(bot)
    await funnel.notify_admin(bot, fmt(texts.ADMIN_BOT_STARTED, mode=settings.payment_mode, test_mode=mode_label))

    problems = check_texts()
    if problems:
        for problem in problems:
            logger.error("texts.py: %s", problem)
        await funnel.notify_admin(bot, fmt(texts.ADMIN_TEXTS_PROBLEMS, problems=esc("\n".join(problems)[:3000])))

    # картинки шагов — заранее в веб-папку (для показа крупно над длинным текстом)
    funnel.publish_step_images()

    # Если токен раньше был подключён к другому сервису через webhook, бот не получал бы сообщений
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramAPIError as exc:
        logger.warning("Не удалось снять webhook: %s", exc)

    scheduler_task = asyncio.create_task(scheduler.run(bot))
    polling = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    )
    try:
        await asyncio.shield(polling)
    except asyncio.CancelledError:
        # Windows: Ctrl+C отменяет main(), а aiogram сам этот сигнал не ловит — останавливаем опрос явно
        if not polling.done():
            try:
                await dp.stop_polling()
            except RuntimeError:  # опрос ещё не успел стартовать
                polling.cancel()
            await asyncio.wait({polling}, timeout=10)
        raise
    finally:
        # Даём дообработаться тому, что уже начато (например, выдаче доступа после оплаты)
        pending = [
            task
            for task in asyncio.all_tasks()
            if task not in (asyncio.current_task(), scheduler_task, polling) and not task.done()
        ]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=20)
            for task in still_running:
                task.cancel()
            await asyncio.gather(*still_running, return_exceptions=True)
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
