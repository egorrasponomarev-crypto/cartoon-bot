"""Оффер, «Программа подробнее», «Частые вопросы». Экраны заменяют друг друга, «Назад» возвращает обратно."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import funnel
import keyboards as kb

router = Router(name="offer")


@router.callback_query(kb.NavCb.filter(F.action == "offer"))
async def cb_offer(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.clear()
    await funnel.show_offer(
        bot, callback.from_user.id, back_step=callback_data.step, pressed_message_id=funnel.pressed_id(callback)
    )


@router.callback_query(kb.NavCb.filter(F.action == "program"))
async def cb_program(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.clear()
    await funnel.show_program(bot, callback.from_user.id, callback_data.step, funnel.pressed_id(callback))


@router.callback_query(kb.NavCb.filter(F.action == "faq"))
async def cb_faq(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.clear()
    await funnel.show_faq(bot, callback.from_user.id, callback_data.step, funnel.pressed_id(callback))
