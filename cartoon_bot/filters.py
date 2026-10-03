"""Фильтры: «это админ?» и «админ отвечает на пересланное сообщение ученика?»."""
from aiogram import Bot
from aiogram.enums import ContentType, MessageEntityType
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

import db
from config import settings

# Карточка ученика (texts.ADMIN_USER_CARD): «Имя (@ник) · ID <code>123</code> · шаг 2 · источник: …»
_CARD_BEFORE_ID = "ID "
_CARD_AFTER_ID = " · шаг"

# Обычные сообщения учеников (не служебные), которые можно переслать автору
FORWARDABLE = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.DOCUMENT,
    ContentType.ANIMATION,
    ContentType.VOICE,
    ContentType.AUDIO,
    ContentType.VIDEO_NOTE,
    ContentType.STICKER,
    ContentType.LOCATION,
    ContentType.CONTACT,
}


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(settings.admin_id) and user is not None and user.id == settings.admin_id


def is_from_bot(message: Message | None, bot_id: int) -> bool:
    return message is not None and message.from_user is not None and message.from_user.id == bot_id


def is_bot_forward(message: Message | None, bot_id: int) -> bool:
    """Сообщение ученика, которое бот переслал админу."""
    return is_from_bot(message, bot_id) and message.forward_origin is not None


def student_in_card(message: Message, bot_id: int) -> int | None:
    """ID ученика из его карточки («Имя (@ник) · ID 123 · шаг 2 · …») в уведомлении бота админу. None — карточки нет.

    Нужен, когда связи «сообщение → ученик» в базе нет: например, уведомление пришло до очистки базы.
    ID — выделенный код между «ID » и « · шаг»: его не подделать именем или ником ученика (они приходят простым
    текстом), и он есть, даже если ученик запретил ссылку на свой аккаунт. Пересланные сообщения не подходят:
    это текст самого ученика, а не его карточка.
    """
    if not is_from_bot(message, bot_id) or message.forward_origin is not None:
        return None
    if message.text is not None:
        text, entities = message.text, message.entities
    else:
        text, entities = message.caption or "", message.caption_entities
    raw = text.encode("utf-16-le")  # места выделений Telegram считает в символах UTF-16
    for entity in entities or []:
        if entity.type != MessageEntityType.CODE:
            continue
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        value = raw[start:end].decode("utf-16-le", errors="ignore")
        if not (value.isascii() and value.isdigit() and len(value) <= 16):  # ID в Telegram — до 16 цифр
            continue
        before = raw[:start].decode("utf-16-le", errors="ignore")
        after = raw[end:].decode("utf-16-le", errors="ignore")
        if before.endswith(_CARD_BEFORE_ID) and after.startswith(_CARD_AFTER_ID):
            return int(value)
    return None


class ReplyToStudent(BaseFilter):
    """Срабатывает, когда админ отвечает (reply) на сообщение, которое бот переслал от ученика, или на его карточку."""

    async def __call__(self, message: Message, bot: Bot) -> bool | dict:
        replied = message.reply_to_message
        if replied is None:
            return False
        student_id = await db.user_by_admin_message(replied.message_id)
        if student_id is None:
            # связи в базе нет (например, базу очищали) — ученика видно по карточке в самом уведомлении
            student_id = student_in_card(replied, bot.id)
        if student_id is None:
            return False
        return {"student_id": student_id}
