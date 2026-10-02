"""Отмечаем активность ученика при каждом сообщении и нажатии кнопки."""
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

import funnel
import keyboards as kb

logger = logging.getLogger(__name__)

# Повторное нажатие той же кнопки за это время считаем случайным двойным нажатием
DOUBLE_TAP_SECONDS = 3

# ID ученика → (сообщение, данные кнопки, когда нажал)
_last_callback: dict[int, tuple[int | None, str | None, float]] = {}


class ActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        chat = data.get("event_chat")

        if isinstance(event, CallbackQuery) and tg_user is not None:
            key = (event.message.message_id if event.message else None, event.data)
            now = time.monotonic()
            previous = _last_callback.get(tg_user.id)
            _last_callback[tg_user.id] = (*key, now)
            if previous is not None and previous[:2] == key and now - previous[2] < DOUBLE_TAP_SECONDS:
                await funnel.answer_callback(event)  # двойное нажатие — второй раз ничего не отправляем
                return None

        if tg_user is not None and not tg_user.is_bot and chat is not None and chat.type == ChatType.PRIVATE:
            try:
                user = await funnel.ensure_user(tg_user)
                # нижнего меню больше нет: у кого оно осталось с прошлой версии — убираем при первом же действии
                old_button = isinstance(event, Message) and event.text in kb.MENU_BUTTONS
                bot = data.get("bot")
                if bot is not None and (old_button or user.get("old_menu") or user.get("menu_msg_id")):
                    await funnel.remove_old_menu(bot, tg_user.id, force=old_button)
            except Exception:
                logger.exception("Не удалось обновить активность пользователя %s", tg_user.id)
        return await handler(event, data)
