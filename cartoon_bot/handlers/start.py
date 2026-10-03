"""/start, кнопки старого нижнего меню, «Отмена», блокировка бота учеником."""
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import KICKED, MEMBER, ChatMemberUpdatedFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

import db
import funnel
import history
import keyboards as kb
import texts
from states import UserStates
from utils import fmt

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user_id = message.from_user.id
    label = (command.args or "").strip()[:64]
    if label:
        # метка источника из ссылки t.me/<бот>?start=<метка>; запоминаем первую
        await db.set_source_if_empty(user_id, label)
    await history.track(user_id, "start", detail=label or None)
    # приветствие (с картинкой из GREETING_MEDIA) и «Начать шаг 1» — вместо прежнего экрана
    await funnel.show_greeting(bot, user_id, message.from_user.first_name)
    await funnel.delete_messages(bot, user_id, [message.message_id])  # само «/start» тоже убираем из чата


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(fmt(texts.MYID, user_id=message.from_user.id))


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(texts.TERMS)


# ---------------------------------------------------------------- кнопки старого нижнего меню
# Меню больше не показывается. У кого оно осталось — нажатие срабатывает, а само меню
# ещё до этих обработчиков убирает ActivityMiddleware (middlewares.py).


@router.message(F.text == texts.BTN_MENU_STEPS)
async def menu_steps(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await funnel.show_steps_menu(bot, message.from_user.id)
    await funnel.delete_messages(bot, message.from_user.id, [message.message_id])  # нажатие меню не копим в чате


@router.message(F.text == texts.BTN_MENU_COURSE)
async def menu_course(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await funnel.show_offer(bot, message.from_user.id)
    await funnel.delete_messages(bot, message.from_user.id, [message.message_id])


@router.message(F.text == texts.BTN_MENU_AUTHOR)
async def menu_author(message: Message, state: FSMContext) -> None:
    chat = kb.contact_kb()
    if chat is not None:  # вопросы пишут в личку (HELP_URL)
        await state.clear()
        await message.answer(texts.ASK_AUTHOR_CHAT, reply_markup=chat)
        return
    await state.set_state(UserStates.waiting_question)
    await message.answer(texts.ASK_AUTHOR, reply_markup=kb.cancel_kb())


@router.callback_query(kb.NavCb.filter(F.action == "mysteps"))
async def cb_mysteps(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await funnel.answer_callback(callback)
    await state.clear()
    await funnel.show_steps_menu(bot, callback.from_user.id, funnel.pressed_id(callback))


@router.callback_query(kb.NavCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await funnel.answer_callback(callback)
    if callback.message is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id, message_id=callback.message.message_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass
    await bot.send_message(callback.from_user.id, texts.CANCELLED, reply_markup=kb.remove_menu())


# ---------------------------------------------------------------- ученик заблокировал / разблокировал бота


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def on_bot_blocked(event: ChatMemberUpdated) -> None:
    if event.chat.type == ChatType.PRIVATE:
        await db.set_blocked(event.from_user.id, True)
        await history.track(event.from_user.id, "blocked")


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_bot_unblocked(event: ChatMemberUpdated) -> None:
    if event.chat.type == ChatType.PRIVATE:
        await db.set_blocked(event.from_user.id, False)
        await history.track(event.from_user.id, "unblocked")
