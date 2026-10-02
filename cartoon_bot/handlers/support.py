"""«Написать автору» и все остальные сообщения учеников — пересылаются админу."""
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import funnel
import keyboards as kb
import texts
from config import settings
from filters import FORWARDABLE
from states import UserStates

router = Router(name="support")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.callback_query(kb.NavCb.filter(F.action == "ask"))
async def cb_ask_author(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.set_state(UserStates.waiting_question)
    await bot.send_message(callback.from_user.id, texts.ASK_AUTHOR, reply_markup=kb.cancel_kb())


@router.message(UserStates.waiting_question, F.content_type.in_(FORWARDABLE))
async def got_question(message: Message, state: FSMContext, bot: Bot) -> None:
    if funnel.claim_album(message):
        await funnel.forward_to_admin(bot, message, None)
        return
    await state.clear()
    delivered = await funnel.forward_to_admin(bot, message, texts.ADMIN_HEADER_QUESTION)
    # заодно убираем старое нижнее меню, если оно осталось
    await message.answer(
        texts.QUESTION_RECEIVED if delivered else texts.SEND_TO_AUTHOR_FAILED, reply_markup=kb.remove_menu()
    )


@router.message(StateFilter(None), F.content_type.in_(FORWARDABLE))
async def any_other_message(message: Message, bot: Bot) -> None:
    """Ученик написал что-то, не нажимая кнопок, — тоже передаём автору."""
    if message.from_user.id == settings.admin_id:
        await message.answer(texts.ADMIN_HINT)
        return
    if funnel.claim_album(message):
        await funnel.forward_to_admin(bot, message, None)
        return
    delivered = await funnel.forward_to_admin(bot, message, texts.ADMIN_HEADER_MESSAGE)
    await message.answer(
        texts.QUESTION_RECEIVED if delivered else texts.SEND_TO_AUTHOR_FAILED, reply_markup=kb.remove_menu()
    )


@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    """Кнопка, которую уже никто не обрабатывает (например, после перезапуска бота)."""
    await callback.answer(texts.BUTTON_EXPIRED, show_alert=True)
