"""Экраны после шага 5: «Что дальше?» → «Покажи» → «Что внутри?» → цена. Экраны заменяют друг друга."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import funnel
import keyboards as kb

router = Router(name="offer")


@router.callback_query(kb.NavCb.filter(F.action == "offer"))
async def cb_offer(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    await funnel.answer_callback(callback)
    await state.clear()
    await funnel.show_offer(
        bot, callback.from_user.id, back_step=callback_data.step, pressed_message_id=funnel.pressed_id(callback)
    )


@router.callback_query(kb.NavCb.filter(F.action.in_(set(funnel.SALES_PAGES))))
async def cb_sales_page(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    """«👀 Покажи», «🔥 Что внутри?» и «👈 Назад» между экранами после шага 5."""
    await funnel.answer_callback(callback)
    await state.clear()
    await funnel.show_sales_page(bot, callback.from_user.id, callback_data.action, funnel.pressed_id(callback))


# «Программа подробнее» и «Частые вопросы» из прежних сообщений: теперь вместо них — «Что внутри?» и цена
@router.callback_query(kb.NavCb.filter(F.action == "program"))
async def cb_program(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await funnel.answer_callback(callback)
    await state.clear()
    await funnel.show_sales_page(bot, callback.from_user.id, "inside", funnel.pressed_id(callback))


@router.callback_query(kb.NavCb.filter(F.action == "faq"))
async def cb_faq(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await funnel.answer_callback(callback)
    await state.clear()
    await funnel.show_offer(bot, callback.from_user.id, pressed_message_id=funnel.pressed_id(callback))
