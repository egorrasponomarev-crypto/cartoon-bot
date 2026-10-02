"""Оплата: звёзды Telegram (stars) или ссылка на оплату (link)."""
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import db
import funnel
import keyboards as kb
import texts
from config import settings
from utils import esc, fmt, format_number, format_price

logger = logging.getLogger(__name__)

router = Router(name="payments")
router.message.filter(F.chat.type == ChatType.PRIVATE)

PAYLOAD_PREFIX = "course"


def _payload_is_discount(payload: str) -> bool | None:
    """Разбирает payload счёта «course:<id>:<1|0>». None — если счёт не наш."""
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != PAYLOAD_PREFIX or parts[2] not in ("0", "1"):
        return None
    return parts[2] == "1"


def _invoice_title() -> str:
    title = texts.INVOICE_TITLE
    return title if len(title) <= 32 else title[:31] + "…"


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(texts.PAYSUPPORT)


@router.callback_query(kb.NavCb.filter(F.action == "pay"))
async def cb_pay(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if user is None:
        return
    if user["paid_at"]:
        await funnel.send_to_screen(bot, user_id, texts.ALREADY_BOUGHT)
        return
    if not user["first_offer_view_at"]:
        # нажал «Оплатить» прямо из напоминания — для статистики считаем, что оффер он видел
        await db.update_user(user_id, first_offer_view_at=db.now())
    rub, stars, is_discount = funnel.current_price(user)

    if settings.payment_mode == "stars":
        try:
            invoice = await bot.send_invoice(
                chat_id=user_id,
                title=_invoice_title(),
                description=texts.INVOICE_DESCRIPTION[:255],
                payload=f"{PAYLOAD_PREFIX}:{user_id}:{1 if is_discount else 0}",
                currency="XTR",
                prices=[LabeledPrice(label=texts.INVOICE_LABEL, amount=stars)],
                provider_token="",
            )
        except TelegramBadRequest as exc:
            logger.error("Telegram не принял счёт на %s звёзд: %s", stars, exc)
            await funnel.send_to_screen(bot, user_id, texts.PAYMENT_UNAVAILABLE, reply_markup=kb.stuck_kb())
            await funnel.notify_admin(bot, fmt(texts.ADMIN_INVOICE_FAILED, amount=stars, error=esc(exc)))
            return
        # счёт — часть текущего экрана: при переходе дальше он уберётся вместе с оффером
        await db.append_screen(user_id, invoice.message_id, "invoice")
        return

    url = settings.payment_link_discount if is_discount else settings.payment_link_full
    if not url:
        await funnel.send_to_screen(bot, user_id, texts.PAYMENT_UNAVAILABLE, reply_markup=kb.stuck_kb())
        await funnel.notify_admin(bot, texts.ADMIN_LINK_MISSING)
        return
    prompt = await bot.send_message(
        user_id,
        fmt(texts.LINK_PAY_PROMPT, price=format_price(rub, stars)),
        reply_markup=kb.link_pay_kb(url, is_discount),
    )
    await db.append_screen(user_id, prompt.message_id, "text")


# ---------------------------------------------------------------- звёзды


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram спрашивает «можно принять оплату?». Отвечать нужно быстро (до 10 секунд)."""
    try:
        is_discount = _payload_is_discount(query.invoice_payload)
        user = await db.get_user(query.from_user.id)
        if is_discount is None or query.currency != "XTR" or user is None:
            await query.answer(ok=False, error_message=texts.PRECHECKOUT_ERROR)
            return
        if user["paid_at"]:
            await query.answer(ok=False, error_message=texts.PRECHECKOUT_ALREADY_PAID)
            return
        expected = settings.price_discount_stars if is_discount else settings.price_full_stars
        if query.total_amount != expected:
            # цены в настройках поменялись после выставления счёта
            await query.answer(ok=False, error_message=texts.PRECHECKOUT_ERROR)
            return
        # счёт со скидкой можно оплатить ещё 15 минут после её окончания
        grace = settings.hours(0.25)
        if is_discount and db.now() > (user["discount_until"] or 0) + grace:
            await query.answer(ok=False, error_message=texts.PRECHECKOUT_EXPIRED)
            return
        await query.answer(ok=True)
    except Exception:
        logger.exception("Ошибка при проверке оплаты")
        await query.answer(ok=False, error_message=texts.PRECHECKOUT_ERROR)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    user_id = message.from_user.id
    amount_text = f"{format_number(payment.total_amount)} {texts.CURRENCY_STARS}"
    try:
        payment_id = await db.add_payment(
            user_id,
            mode="stars",
            amount=payment.total_amount,
            currency=payment.currency,
            is_discount=bool(_payload_is_discount(payment.invoice_payload)),
            status="paid",
            charge_id=payment.telegram_payment_charge_id,
        )
        if payment_id is None:
            logger.info("Оплата %s уже была обработана", payment.telegram_payment_charge_id)
            return
        await funnel.grant_access(bot, user_id, amount_text=amount_text, method_text=texts.ADMIN_METHOD_STARS)
    except Exception:
        # деньги уже списаны — админ обязательно должен об этом узнать
        logger.exception("Ошибка при выдаче доступа после оплаты %s", payment.telegram_payment_charge_id)
        user = await db.get_user(user_id)
        await funnel.notify_admin(
            bot,
            fmt(
                texts.ADMIN_PAYMENT_PROCESS_FAILED,
                user=funnel.user_card(user, message.from_user),
                amount=amount_text,
                charge_id=esc(payment.telegram_payment_charge_id),
                user_id=user_id,
            ),
            about_user=user_id,
        )


# ---------------------------------------------------------------- ссылка на оплату


@router.callback_query(kb.NavCb.filter(F.action.in_({"paid", "paid_d", "paid_f"})))
async def cb_i_paid(callback: CallbackQuery, callback_data: kb.NavCb, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if user is None:
        return
    if user["paid_at"]:
        await funnel.send_to_screen(bot, user_id, texts.ALREADY_BOUGHT)
        return
    if await db.pending_payment(user_id):
        # заявка уже у админа — второй раз не дёргаем
        await funnel.send_to_screen(bot, user_id, texts.LINK_PAID_THANKS)
        return
    # Сумма — по той ссылке, которую ученику показали (скидка могла закончиться, пока он платил)
    if callback_data.action == "paid_d":
        rub, is_discount = settings.price_discount_rub, True
    elif callback_data.action == "paid_f":
        rub, is_discount = settings.price_full_rub, False
    else:
        rub, _, is_discount = funnel.current_price(user)
    payment_id = await db.add_payment(
        user_id, mode="link", amount=rub, currency="RUB", is_discount=is_discount, status="pending"
    )
    await funnel.notify_admin(
        bot,
        fmt(
            texts.ADMIN_LINK_REQUEST,
            user=funnel.user_card(user),
            amount=f"{format_number(rub)} {texts.CURRENCY_RUB}",
            price_kind=texts.ADMIN_PRICE_KIND_DISCOUNT if is_discount else texts.ADMIN_PRICE_KIND_FULL,
        ),
        reply_markup=kb.admin_payment_kb(payment_id),
        about_user=user_id,
    )
    await funnel.send_to_screen(bot, user_id, texts.LINK_PAID_THANKS)
