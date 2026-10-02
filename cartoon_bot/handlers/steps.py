"""Шаги 1–5: переход вперёд/назад и «Забрать курс». Каждый новый шаг заменяет предыдущий экран.

Кнопки «Отправить результат» и «Застрял» больше не показываются, но их обработчики оставлены
для старых сообщений, которые уже есть у учеников в чате.
"""
import contextlib

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import funnel
import keyboards as kb
import texts
from filters import FORWARDABLE
from states import UserStates

router = Router(name="steps")
router.message.filter(F.chat.type == ChatType.PRIVATE)

VALID_STEP = F.step.in_({1, 2, 3, 4, 5})


@router.callback_query(kb.StepCb.filter((F.action == "open") & F.step.in_({0, 1, 2, 3, 4, 5})))
async def cb_open_step(callback: CallbackQuery, callback_data: kb.StepCb, state: FSMContext, bot: Bot) -> None:
    """Открыть шаг (0 — приветствие): кнопки «👈 Назад», список шагов, «Продолжить шаг N» в напоминании."""
    await callback.answer()
    await state.clear()
    await funnel.open_step(bot, callback.from_user.id, callback_data.step, funnel.pressed_id(callback))


@router.callback_query(kb.StepCb.filter((F.action == "done") & VALID_STEP))
async def cb_step_done(callback: CallbackQuery, callback_data: kb.StepCb, state: FSMContext, bot: Bot) -> None:
    """Кнопка «👉 Шаг N+1…»."""
    await callback.answer()
    await state.clear()
    await funnel.complete_step(bot, callback.from_user.id, callback_data.step, funnel.pressed_id(callback))


@router.callback_query(kb.StepCb.filter(F.action == "finish"))
async def cb_finish(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.clear()
    await funnel.finish_course(bot, callback.from_user.id, funnel.pressed_id(callback))


@router.callback_query(kb.StepCb.filter((F.action == "file") & VALID_STEP))
async def cb_step_file(callback: CallbackQuery, callback_data: kb.StepCb, bot: Bot) -> None:
    """Кнопка «📥 Скачать…» под шагом (например, мастер-промпт на шаге 1)."""
    # пока файл отправляется, на кнопке «часики»; не получилось — короткое окошко вместо тишины
    sent = await funnel.send_step_download(bot, callback.from_user.id, callback_data.step)
    # если отправка затянулась (Telegram просил подождать), ответ на нажатие уже может не приниматься
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer(None if sent else texts.DOWNLOAD_FAILED, show_alert=not sent)


@router.callback_query(kb.StepCb.filter((F.action == "stuck") & VALID_STEP))
async def cb_stuck(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Старая кнопка «Застрял» — теперь просто предлагаем написать (кнопка «Нужна помощь»)."""
    await callback.answer()
    await state.clear()
    await bot.send_message(callback.from_user.id, texts.HELP_PROMPT, reply_markup=kb.help_kb())


@router.callback_query(kb.StepCb.filter((F.action == "result") & VALID_STEP))
async def cb_send_result(callback: CallbackQuery, callback_data: kb.StepCb, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.set_state(UserStates.waiting_result)
    await state.update_data(step=callback_data.step)
    await bot.send_message(callback.from_user.id, texts.RESULT_PROMPT, reply_markup=kb.cancel_kb())


@router.message(UserStates.waiting_result, F.content_type.in_(FORWARDABLE))
async def got_result(message: Message, state: FSMContext, bot: Bot) -> None:
    if funnel.claim_album(message):
        await funnel.forward_to_admin(bot, message, None)
        return
    data = await state.get_data()
    await state.clear()
    delivered = await funnel.forward_to_admin(bot, message, texts.ADMIN_HEADER_RESULT, step=data.get("step"))
    # заодно убираем старое нижнее меню, если оно осталось
    await message.answer(
        texts.RESULT_RECEIVED if delivered else texts.SEND_TO_AUTHOR_FAILED, reply_markup=kb.remove_menu()
    )
