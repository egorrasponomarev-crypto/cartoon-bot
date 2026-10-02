"""Фильтры: «это админ?» и «админ отвечает на пересланное сообщение ученика?»."""
from aiogram.enums import ContentType
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

import db
from config import settings

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


class ReplyToStudent(BaseFilter):
    """Срабатывает, когда админ отвечает (reply) на сообщение, которое бот переслал от ученика."""

    async def __call__(self, message: Message) -> bool | dict:
        replied = message.reply_to_message
        if replied is None:
            return False
        student_id = await db.user_by_admin_message(replied.message_id)
        if student_id is None:
            return False
        return {"student_id": student_id}
