"""Команды и функции админа. Работают только для ADMIN_ID из .env."""
import asyncio
import json
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageOriginChannel, MessageOriginHiddenUser, MessageOriginUser

import db
import funnel
import keyboards as kb
import texts
from config import settings
from filters import FORWARDABLE, IsAdmin, ReplyToStudent
from states import AdminStates
from utils import esc, fmt, format_number

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
router.callback_query.filter(IsAdmin())

MEDIA_TYPES = {
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.ANIMATION,
    ContentType.DOCUMENT,
    ContentType.VIDEO_NOTE,
    ContentType.AUDIO,
    ContentType.VOICE,
    ContentType.STICKER,
}

_broadcast_task: asyncio.Task | None = None
_broadcast_stop_requested = False  # True — рассылку остановил админ (а не перезапуск бота)


def _not_menu_or_command(message: Message) -> bool:
    """Обычное сообщение админа — не нажатие нижнего меню, не команда и не служебное (например, оплата)."""
    if message.content_type not in FORWARDABLE:
        return False
    text = message.text or ""
    return text not in kb.MENU_BUTTONS and not text.startswith("/")


async def _find_student(message: Message, command: CommandObject, example: str) -> dict | None:
    """Ученик из команды: /grant 123456789 или /grant @username. Не нашёлся — сам отвечает админу и вернёт None."""
    args = (command.args or "").split()
    if not args:
        await message.answer(fmt(texts.ADMIN_BAD_ID, example=example))
        return None
    arg = args[0]
    if arg.isdigit():
        user = await db.get_user(int(arg))
    elif arg.lstrip("@").replace("_", "").isalnum():
        user = await db.find_user_by_username(arg)
    else:
        await message.answer(fmt(texts.ADMIN_BAD_ID, example=example))
        return None
    if user is None:
        await message.answer(fmt(texts.ADMIN_USER_NOT_FOUND, user_id=esc(arg)))
    return user


def _pct(part: int, whole: int) -> str:
    return f"{round(part * 100 / whole)}%" if whole else "0%"


# ================================================================ команды


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(texts.ADMIN_HELP)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    s = await db.stats()
    total = s["total"]

    source_rows = s["sources"]
    source_lines = [
        fmt(
            texts.ADMIN_STATS_SOURCE_LINE,
            source=esc(row["source"]) if row["source"] else texts.ADMIN_NO_SOURCE,
            count=row["count"],
            paid=row["paid"],
            pct=_pct(row["paid"], row["count"]),
        )
        for row in source_rows[:40]
    ]
    if len(source_rows) > 40:
        source_lines.append(fmt(texts.ADMIN_STATS_MORE, count=len(source_rows) - 40))

    funnel_lines = []
    previous = total
    for step in range(1, 6):
        count = s["steps"][step]
        funnel_lines.append(
            fmt(texts.ADMIN_STATS_STEP_LINE, step=step, count=count, pct=_pct(count, total), pct_prev=_pct(count, previous))
        )
        previous = count

    revenue_parts = []
    for row in s["revenue"]:
        amount = row["total"] or 0
        if amount <= 0:
            continue
        signs = {"XTR": texts.CURRENCY_STARS, "RUB": texts.CURRENCY_RUB}
        sign = signs.get(row["currency"], row["currency"])
        revenue_parts.append(f"{format_number(amount)} {sign}")

    await message.answer(
        fmt(
            texts.ADMIN_STATS,
            total=total,
            blocked=s["blocked"],
            sources="\n".join(source_lines) or "—",
            funnel="\n".join(funnel_lines),
            finished=s["finished"],
            finished_pct=_pct(s["finished"], total),
            offer_viewed=s["offer_viewed"],
            offer_viewed_pct=_pct(s["offer_viewed"], total),
            paid=s["paid"],
            paid_pct=_pct(s["paid"], total),
            paid_of_offer_pct=_pct(s["paid"], s["offer_viewed"]),
            revenue=" + ".join(revenue_parts) or texts.ADMIN_STATS_NO_REVENUE,
        )
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user_id = message.from_user.id
    # убираем из чата текущий экран (шаг, оффер) и старую подсказку с меню — проверка начнётся с чистого листа
    user = await db.get_user(user_id)
    stale = [message_id for message_id, _ in await db.get_screen(user_id)]
    if user and user.get("menu_msg_id"):
        stale.append(user["menu_msg_id"])
    await funnel.delete_messages(bot, user_id, stale)
    await db.delete_user(user_id)
    # заодно убираем старое нижнее меню, если оно осталось
    await message.answer(texts.ADMIN_RESET_DONE, reply_markup=kb.remove_menu())


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot) -> None:
    user = await _find_student(message, command, "/refund 123456789 или /refund @username")
    if user is None:
        return
    user_id = user["user_id"]
    payment = await db.last_paid_payment(user_id)
    if payment is None:
        await message.answer(texts.ADMIN_REFUND_NOT_FOUND)
        return

    if payment["mode"] == "stars" and payment["telegram_payment_charge_id"]:
        try:
            await bot.refund_star_payment(
                user_id=user_id, telegram_payment_charge_id=payment["telegram_payment_charge_id"]
            )
        except Exception as exc:  # noqa: BLE001 — покажем админу причину
            await message.answer(fmt(texts.ADMIN_REFUND_FAILED, error=esc(str(exc) or exc.__class__.__name__)))
            return
        await db.set_payment_status(payment["id"], "refunded")
        kick_error = await funnel.revoke_access(bot, user_id)
        await funnel.send_text(bot, user_id, texts.REFUND_DONE_USER)
        await message.answer(fmt(texts.ADMIN_REFUND_DONE_STARS, amount=format_number(payment["amount"])))
    else:
        await db.set_payment_status(payment["id"], "refunded")
        kick_error = await funnel.revoke_access(bot, user_id)
        await message.answer(texts.ADMIN_REFUND_DONE_MANUAL)

    if kick_error:
        await message.answer(fmt(texts.ADMIN_KICK_FAILED, error=esc(kick_error)))


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot) -> None:
    user = await _find_student(message, command, "/grant 123456789 или /grant @username")
    if user is None:
        return
    user_id = user["user_id"]
    if not user["paid_at"]:
        await db.add_payment(user_id, mode="manual", amount=0, currency="RUB", is_discount=False, status="paid")
    link_created, delivered = await funnel.grant_access(
        bot, user_id, notify_payment=False, report_undelivered=False
    )
    if not link_created:
        result = texts.ADMIN_GRANT_NO_LINK
    elif not delivered:
        result = texts.ADMIN_GRANT_NOT_DELIVERED
    else:
        result = texts.ADMIN_GRANT_DONE
    await message.answer(fmt(result, user_id=user_id))


@router.message(Command("fileid"))
async def cmd_fileid(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.fileid)
    await message.answer(texts.ADMIN_FILEID_PROMPT, reply_markup=kb.cancel_kb())


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if _broadcast_task is not None and not _broadcast_task.done():
        await message.answer(texts.ADMIN_BROADCAST_BUSY)
        return
    await state.set_state(AdminStates.broadcast_segment)
    await message.answer(texts.ADMIN_BROADCAST_CHOOSE, reply_markup=kb.broadcast_segment_kb())


# ================================================================ ответ ученику


@router.message(ReplyToStudent())
async def reply_to_student(message: Message, bot: Bot, student_id: int) -> None:
    result = texts.ADMIN_REPLY_FAILED
    for _ in range(3):
        try:
            await bot.copy_message(chat_id=student_id, from_chat_id=message.chat.id, message_id=message.message_id)
            result = texts.ADMIN_REPLY_SENT
            break
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            await db.set_blocked(student_id, True)
            break
        except Exception as exc:  # noqa: BLE001 — покажем админу настоящую причину
            result = fmt(texts.ADMIN_REPLY_ERROR, error=esc(str(exc) or exc.__class__.__name__))
            break
    await message.reply(result)


# ================================================================ file_id и ID канала


def _file_info(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:  # проверяем до document: у GIF заполнены оба поля
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    return None


async def _answer_file_id(message: Message) -> None:
    info = _file_info(message)
    if info is None:
        await message.answer(texts.ADMIN_FILEID_UNSUPPORTED)
        return
    kind, file_id = info
    if kind in ("photo", "video", "animation", "document"):
        snippet = esc(json.dumps({"type": kind, "file_id": file_id}))
    else:
        snippet = texts.ADMIN_FILEID_NOT_FOR_STEPS
    await message.reply(fmt(texts.ADMIN_FILEID_RESULT, kind=kind, file_id=file_id, snippet=snippet))


@router.message(AdminStates.fileid, F.content_type.in_(MEDIA_TYPES))
async def fileid_media(message: Message) -> None:
    await _answer_file_id(message)


@router.message(AdminStates.fileid, _not_menu_or_command)
async def fileid_not_media(message: Message) -> None:
    await message.answer(texts.ADMIN_FILEID_UNSUPPORTED)


@router.message(StateFilter(None), F.forward_origin)
async def forwarded_from_channel(message: Message) -> None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        await message.answer(fmt(texts.ADMIN_CHANNEL_ID, chat_id=origin.chat.id))
    elif message.content_type in MEDIA_TYPES:
        await _answer_file_id(message)
    elif isinstance(origin, MessageOriginUser):
        # пересланное сообщение ученика (например, предзапись из личного чата) — кто это и как выдать доступ
        student = await db.get_user(origin.sender_user.id)
        template = texts.ADMIN_FORWARDED_STUDENT if student else texts.ADMIN_FORWARDED_NOT_IN_BOT
        await message.answer(
            fmt(template, user=funnel.user_card(student, origin.sender_user), user_id=origin.sender_user.id)
        )
    elif isinstance(origin, MessageOriginHiddenUser):
        await message.answer(texts.ADMIN_FORWARDED_HIDDEN)
    else:
        await message.answer(texts.ADMIN_HINT)


@router.message(StateFilter(None), F.content_type.in_(MEDIA_TYPES))
async def admin_media(message: Message) -> None:
    await _answer_file_id(message)


# ================================================================ рассылка


@router.callback_query(AdminStates.broadcast_segment, kb.BroadcastCb.filter(F.action.in_({"all", "paid", "unpaid"})))
async def broadcast_segment(callback: CallbackQuery, callback_data: kb.BroadcastCb, state: FSMContext, bot: Bot) -> None:
    await funnel.answer_callback(callback)
    await state.update_data(segment=callback_data.action)
    await state.set_state(AdminStates.broadcast_content)
    await bot.send_message(
        callback.from_user.id, texts.ADMIN_BROADCAST_SEND_CONTENT, reply_markup=kb.broadcast_cancel_kb()
    )


@router.message(AdminStates.broadcast_content, _not_menu_or_command)
async def broadcast_content(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user_ids = await db.users_for_broadcast(data.get("segment", "all"))
    if not user_ids:
        await state.clear()
        await message.answer(texts.ADMIN_BROADCAST_EMPTY)
        return
    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(AdminStates.broadcast_confirm)
    await bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id, message_id=message.message_id)
    await message.answer(
        fmt(texts.ADMIN_BROADCAST_PREVIEW, count=len(user_ids)), reply_markup=kb.broadcast_confirm_kb()
    )


@router.message(StateFilter(AdminStates.broadcast_segment, AdminStates.broadcast_confirm), _not_menu_or_command)
async def broadcast_use_buttons(message: Message) -> None:
    await message.answer(texts.ADMIN_BROADCAST_USE_BUTTONS)


@router.callback_query(AdminStates.broadcast_confirm, kb.BroadcastCb.filter(F.action == "send"))
async def broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    global _broadcast_task, _broadcast_stop_requested
    data = await state.get_data()
    await state.clear()
    await funnel.answer_callback(callback)
    if _broadcast_task is not None and not _broadcast_task.done():
        await bot.send_message(callback.from_user.id, texts.ADMIN_BROADCAST_BUSY)
        return
    # рассылка пошла — кнопки «Отправить» и «Отмена» под превью больше не нужны
    await _remove_pressed_buttons(callback, bot)
    user_ids = await db.users_for_broadcast(data.get("segment", "all"))
    _broadcast_stop_requested = False
    _broadcast_task = asyncio.create_task(
        run_broadcast(bot, user_ids, data["from_chat_id"], data["message_id"])
    )
    await bot.send_message(callback.from_user.id, texts.ADMIN_BROADCAST_STARTED, reply_markup=kb.broadcast_stop_kb())


@router.callback_query(kb.BroadcastCb.filter(F.action == "cancel"))
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await funnel.answer_callback(callback)
    if _broadcast_task is not None and not _broadcast_task.done():
        # «Отмена» под старым сообщением не останавливает уже идущую рассылку — не делаем вид, что остановила
        await bot.send_message(callback.from_user.id, texts.ADMIN_BROADCAST_RUNNING)
        return
    await bot.send_message(callback.from_user.id, texts.ADMIN_BROADCAST_CANCELLED)


@router.callback_query(kb.BroadcastCb.filter(F.action == "stop"))
async def broadcast_stop(callback: CallbackQuery, bot: Bot) -> None:
    """Кнопка «Остановить рассылку»: итог (сколько успело уйти) пришлёт сама рассылка."""
    global _broadcast_stop_requested
    await _remove_pressed_buttons(callback, bot)
    if _broadcast_task is None or _broadcast_task.done():
        await funnel.answer_callback(callback, texts.ADMIN_BROADCAST_NOT_RUNNING, show_alert=True)
        return
    await funnel.answer_callback(callback)
    _broadcast_stop_requested = True
    _broadcast_task.cancel()


async def _remove_pressed_buttons(callback: CallbackQuery, bot: Bot) -> None:
    message_id = funnel.pressed_id(callback)
    if message_id is not None:
        await funnel.remove_buttons(bot, callback.from_user.id, [message_id])


async def run_broadcast(bot: Bot, user_ids: list[int], from_chat_id: int, message_id: int) -> None:
    """Отправляет копию сообщения всем из списка с паузами, чтобы не упереться в лимиты Telegram."""
    ok = failed = blocked = 0
    try:
        for user_id in user_ids:
            for _ in range(3):
                try:
                    await bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
                    ok += 1
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                except TelegramForbiddenError:
                    blocked += 1
                    failed += 1
                    await db.set_blocked(user_id, True)
                    break
                except TelegramBadRequest as exc:
                    failed += 1
                    if "chat not found" in str(exc).lower():
                        blocked += 1
                        await db.set_blocked(user_id, True)
                    break
                except Exception as exc:  # noqa: BLE001 — сбой связи и т.п.: идём дальше по списку
                    logger.warning("Рассылка: не удалось отправить %s: %s", user_id, exc)
                    failed += 1
                    break
            else:
                failed += 1
            await asyncio.sleep(settings.broadcast_delay)
    finally:
        # кому не успели отправить: рассылку остановил админ, бота перезапустили или случилась ошибка
        left = len(user_ids) - ok - failed
        if left <= 0:
            template = texts.ADMIN_BROADCAST_DONE
        elif _broadcast_stop_requested:
            template = texts.ADMIN_BROADCAST_STOPPED
        else:
            template = texts.ADMIN_BROADCAST_INTERRUPTED
        logger.info("Рассылка: доставлено %s, не доставлено %s, не отправлено %s", ok, failed, max(left, 0))
        await funnel.notify_admin(bot, fmt(template, ok=ok, failed=failed, blocked=blocked, left=left))


# ================================================================ подтверждение оплаты (режим link)


@router.callback_query(kb.AdminPayCb.filter())
async def admin_payment_decision(callback: CallbackQuery, callback_data: kb.AdminPayCb, bot: Bot) -> None:
    payment = await db.get_payment(callback_data.payment_id)
    if payment is None or payment["status"] != "pending":
        await funnel.answer_callback(callback, texts.ADMIN_ALREADY_PROCESSED, show_alert=True)
        return
    user_id = payment["user_id"]
    user = await db.get_user(user_id)
    if user is not None and user["paid_at"]:
        # доступ уже выдан (например, через /grant) — старую заявку просто закрываем
        await db.set_payment_status(payment["id"], "duplicate", only_if="pending")
        await funnel.answer_callback(callback)
        result = texts.ADMIN_PAYMENT_ALREADY_PAID
    elif callback_data.action == "ok":
        if not await db.set_payment_status(payment["id"], "paid", only_if="pending"):
            await funnel.answer_callback(callback, texts.ADMIN_ALREADY_PROCESSED, show_alert=True)
            return
        await funnel.answer_callback(callback)
        link_created, delivered = await funnel.grant_access(
            bot,
            user_id,
            amount_text=f"{format_number(payment['amount'])} {texts.CURRENCY_RUB}",
            method_text=texts.ADMIN_METHOD_LINK,
            report_undelivered=False,
        )
        if not link_created:
            result = fmt(texts.ADMIN_GRANT_NO_LINK, user_id=user_id)
        elif not delivered:
            result = fmt(texts.ADMIN_GRANT_NOT_DELIVERED, user_id=user_id)
        else:
            result = texts.ADMIN_PAYMENT_CONFIRMED
    else:
        if not await db.set_payment_status(payment["id"], "rejected", only_if="pending"):
            await funnel.answer_callback(callback, texts.ADMIN_ALREADY_PROCESSED, show_alert=True)
            return
        await funnel.answer_callback(callback)
        await funnel.send_text(bot, user_id, texts.LINK_PAYMENT_NOT_FOUND, kb.stuck_kb())
        result = texts.ADMIN_PAYMENT_REJECTED

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(f"{callback.message.html_text}\n\n{result}", reply_markup=None)
        except TelegramBadRequest:
            await bot.send_message(callback.from_user.id, result)
    else:
        await bot.send_message(callback.from_user.id, result)
