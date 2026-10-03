"""«Написать автору» и все остальные сообщения учеников — пересылаются админу."""
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import funnel
import history
import keyboards as kb
import texts
from config import settings
from filters import FORWARDABLE
from states import UserStates

router = Router(name="support")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.callback_query(kb.NavCb.filter(F.action == "ask"))
async def cb_ask_author(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    await funnel.answer_callback(callback)
    user_id = callback.from_user.id
    # step=1 — «❓ Есть вопрос» на экране цены; остальные — «Написать автору» / «Нужна помощь»
    await history.track(user_id, "ask", step=1 if callback_data.step == 1 else None)
    chat = kb.contact_kb()
    if chat is not None:
        # вопросы пишут в личку (HELP_URL); сообщение уберётся вместе с экраном при следующем переходе
        await state.clear()
        await funnel.send_to_screen(bot, user_id, texts.ASK_AUTHOR_CHAT, reply_markup=chat)
        return
    # ссылки на личку нет — вопрос пишут прямо боту, он придёт админу
    await state.set_state(UserStates.waiting_question)
    await bot.send_message(user_id, texts.ASK_AUTHOR, reply_markup=kb.cancel_kb())


@router.message(UserStates.waiting_question, F.content_type.in_(FORWARDABLE))
async def got_question(message: Message, state: FSMContext, bot: Bot) -> None:
    if funnel.claim_album(message):
        await funnel.forward_to_admin(bot, message, None)
        return
    await state.clear()
    delivered = await funnel.forward_to_admin(bot, message, texts.ADMIN_HEADER_QUESTION)
    await history.track(message.from_user.id, "question" if delivered else "question_failed")
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
    await history.track(message.from_user.id, "question" if delivered else "question_failed")
    await message.answer(
        texts.QUESTION_RECEIVED if delivered else texts.SEND_TO_AUTHOR_FAILED, reply_markup=kb.remove_menu()
    )


@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    """Кнопка, которую уже никто не обрабатывает (например, после перезапуска бота)."""
    await funnel.answer_callback(callback, texts.BUTTON_EXPIRED, show_alert=True)
