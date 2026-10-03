"""Кнопки бота. Названия кнопок берутся из texts.py."""
import re
from urllib.parse import quote

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

import texts
from config import settings
from utils import discount_price_text, fmt, full_price_text

# Личка для вопросов (HELP_URL в .env, сейчас @nanopapa): туда ведут «Нужна помощь», «💬 Написать»
# после «Есть вопрос» и чат с готовой заявкой после «Вступить». Если HELP_URL не задан, при запуске бот
# подставляет сюда чат с админом. Пусто — вопросы пишут прямо боту (они приходят админу).
_help_url: str = settings.help_url


def set_help_url(url: str) -> None:
    global _help_url
    _help_url = url

# ---------------------------------------------------------------- данные кнопок


class StepCb(CallbackData, prefix="st"):
    action: str  # open (step=0 — приветствие) | done | finish | file (скачать файл шага) | result | stuck
    step: int


class NavCb(CallbackData, prefix="nav"):
    # pitch | product | inside — экраны после шага 5, offer — цена, pay — «Вступить»/«Оплатить»,
    # ask — вопрос автору; program, faq — старые кнопки из прежних сообщений; paid_d | paid_f | cancel | mysteps
    action: str
    # pay: 1 — на кнопке «Вступить» стояла цена со скидкой; ask: 1 — «Есть вопрос» на экране цены;
    # в старых кнопках offer — с какого шага открыли оффер
    step: int = 0

    @classmethod
    def unpack(cls, value: str) -> "NavCb":
        # кнопки из сообщений, отправленных до появления «Назад» (например «nav:pay»), — без номера шага
        if value.count(cls.__separator__) == 1:
            value += cls.__separator__ + "0"
        return super().unpack(value)


class AdminPayCb(CallbackData, prefix="ap"):
    action: str  # ok | no
    payment_id: int


class BroadcastCb(CallbackData, prefix="bc"):
    action: str  # all | paid | unpaid | send | cancel | stop


def _btn(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


# ---------------------------------------------------------------- для учеников

# Кнопки старого нижнего меню. Само меню больше не показывается, но у кого оно осталось —
# нажатия по-прежнему работают (а меню при этом убирается).
MENU_BUTTONS = (texts.BTN_MENU_STEPS, texts.BTN_MENU_COURSE, texts.BTN_MENU_AUTHOR)


def remove_menu() -> ReplyKeyboardRemove:
    """Убирает нижнее меню (клавиатуру под полем ввода), если оно осталось с прошлой версии бота."""
    return ReplyKeyboardRemove()


def start_kb() -> InlineKeyboardMarkup:
    """Под приветствием — всегда «🚀 Начать», к шагу 1 (бот не предлагает «продолжить с места»)."""
    return _kb([_btn(texts.BTN_START_STEP1, StepCb(action="open", step=1))])


def help_button() -> InlineKeyboardButton:
    """«Нужна помощь»: ссылка на чат с админом, а если ссылки нет — вопрос автору внутри бота."""
    if _help_url:
        return InlineKeyboardButton(text=texts.BTN_HELP, url=_help_url)
    return _btn(texts.BTN_HELP, NavCb(action="ask"))


def help_kb() -> InlineKeyboardMarkup:
    return _kb([help_button()])


def contact_kb() -> InlineKeyboardMarkup | None:
    """Кнопка «💬 Написать» в личку для вопросов. None — ссылки нет: вопрос пишут прямо боту."""
    if _help_url:
        return _kb([InlineKeyboardButton(text=texts.BTN_WRITE_CHAT, url=_help_url)])
    return None


def preorder_url() -> str | None:
    """Чат с автором (тот же, что у «Нужна помощь») с готовым сообщением о предзаписи.

    Ссылка вида https://t.me/username?text=... — Telegram откроет чат и впишет текст в поле ввода.
    None — если ссылки на чат нет (у админа нет username и HELP_URL не задан).
    """
    match = re.fullmatch(r"https?://(?:t|telegram)\.me/([A-Za-z0-9_]{4,32})/?", _help_url or "")
    if match is None:
        return None
    return f"https://t.me/{match.group(1)}?text={quote(texts.PREORDER_MESSAGE)}"


def preorder_kb(url: str) -> InlineKeyboardMarkup:
    """Кнопка, которая откроет чат с автором с готовым сообщением о предзаписи."""
    return _kb([InlineKeyboardButton(text=texts.BTN_PREORDER_CHAT, url=url)])


def pay_button(discount_active: bool = True) -> InlineKeyboardButton:
    """«🔥 Вступить за 2 490 ₽» (режим предзаписи; цена — та, что действует сейчас) или «Оплатить».

    Нажатие сначала приходит в бот: оно попадает в историю ученика, а админу приходит уведомление.
    Потом бот даёт кнопку, которая откроет чат с автором (preorder_kb).
    """
    if settings.payment_mode == "preorder":
        price = discount_price_text() if discount_active else full_price_text()
        # запоминаем, какая цена была на кнопке: если скидка закончится, а кнопка останется в старом
        # сообщении, бот предупредит ученика и покажет актуальную цену
        return _btn(fmt(texts.BTN_JOIN, price=price), NavCb(action="pay", step=1 if discount_active else 0))
    return _btn(texts.BTN_PAY, NavCb(action="pay"))


def step_kb(step: int) -> InlineKeyboardMarkup:
    if step < 5:
        main = _btn(texts.BTN_NEXT_STEP.get(step, texts.BTN_DONE), StepCb(action="done", step=step))
    else:
        # шаг 5: «🔥 Что дальше?» — практикум пройден, дальше экраны про полный курс
        main = _btn(texts.BTN_WHATS_NEXT, StepCb(action="finish", step=step))
    # «Назад» с шага 1 ведёт к приветствию (шаг 0)
    back = _btn(texts.BTN_BACK, StepCb(action="open", step=step - 1))
    download = (getattr(texts, "STEP_DOWNLOADS", None) or {}).get(step)
    return _kb(
        # кнопка скачивания файла шага (например, мастер-промпта) — первой
        [_btn(download["button"], StepCb(action="file", step=step))]
        if isinstance(download, dict) and download.get("button")
        else [],
        [main],
        [back, help_button()],
    )


def steps_menu_kb() -> InlineKeyboardMarkup:
    """Список шагов (кнопка меню «📚 Мои шаги»): можно перейти к любому."""
    rows = [[_btn(texts.BTN_STEPS_MENU[n], StepCb(action="open", step=n))] for n in sorted(texts.BTN_STEPS_MENU)]
    return _kb(*rows, [_btn(texts.BTN_FULL_COURSE, NavCb(action="offer"))])


def stuck_kb() -> InlineKeyboardMarkup:
    return _kb([_btn(texts.BTN_WRITE_AUTHOR, NavCb(action="ask"))])


def cancel_kb() -> InlineKeyboardMarkup:
    return _kb([_btn(texts.BTN_CANCEL, NavCb(action="cancel"))])


# ---------------------------------------------------------------- экраны после шага 5


def pitch_kb() -> InlineKeyboardMarkup:
    """«Что дальше?»: дальше — «👀 Покажи», «Назад» — к шагу 5."""
    return _kb(
        [_btn(texts.BTN_SHOW, NavCb(action="product"))],
        [_btn(texts.BTN_BACK, StepCb(action="open", step=5))],
    )


def product_kb() -> InlineKeyboardMarkup:
    """«Покажи» (что за курс): дальше — «🔥 Что внутри?»."""
    return _kb(
        [_btn(texts.BTN_INSIDE, NavCb(action="inside"))],
        [_btn(texts.BTN_BACK, NavCb(action="pitch"))],
    )


def inside_kb() -> InlineKeyboardMarkup:
    """«Что внутри?»: дальше — «💳 Сколько стоит?» (экран цены)."""
    return _kb(
        [_btn(texts.BTN_PRICE, NavCb(action="offer"))],
        [_btn(texts.BTN_BACK, NavCb(action="product"))],
    )


def offer_kb(discount_active: bool = True) -> InlineKeyboardMarkup:
    """Экран цены: «🔥 Вступить за …» (цена — та, что действует сейчас), «❓ Есть вопрос», «Назад»."""
    return _kb(
        [pay_button(discount_active)],
        [_btn(texts.BTN_QUESTION, NavCb(action="ask", step=1))],
        [_btn(texts.BTN_BACK, NavCb(action="inside"))],
    )


def back_kb(step: int) -> InlineKeyboardMarkup | None:
    """Одна кнопка «👈 Назад» к шагу step (0 — к приветствию); None, если возвращаться некуда."""
    return _kb([_btn(texts.BTN_BACK, StepCb(action="open", step=step))]) if step else None


def remind_step_kb(step: int, with_help: bool) -> InlineKeyboardMarkup:
    return _kb(
        [_btn(fmt(texts.BTN_CONTINUE_STEP, step=step), StepCb(action="open", step=step))],
        [help_button()] if with_help else [],
    )


def offer_reminder_kb(with_author: bool = False, discount_active: bool = True) -> InlineKeyboardMarkup:
    """Под напоминаниями про курс: «Вступить за …» (цена — та, что действует сейчас) и «🎓 Полный курс»."""
    return _kb(
        [pay_button(discount_active)],
        [_btn(texts.BTN_FULL_COURSE, NavCb(action="offer"))],
        [_btn(texts.BTN_WRITE_AUTHOR, NavCb(action="ask"))] if with_author else [],
    )


def link_pay_kb(url: str, is_discount: bool) -> InlineKeyboardMarkup:
    # в кнопке «Я оплатил» запоминаем, какую ссылку показали: со скидкой или без
    return _kb(
        [InlineKeyboardButton(text=texts.BTN_PAY_LINK, url=url)],
        [_btn(texts.BTN_I_PAID, NavCb(action="paid_d" if is_discount else "paid_f"))],
    )


# ---------------------------------------------------------------- для админа


def admin_payment_kb(payment_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            _btn(texts.BTN_ADMIN_CONFIRM, AdminPayCb(action="ok", payment_id=payment_id)),
            _btn(texts.BTN_ADMIN_REJECT, AdminPayCb(action="no", payment_id=payment_id)),
        ]
    )


def broadcast_segment_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn(texts.BTN_SEGMENT_ALL, BroadcastCb(action="all"))],
        [_btn(texts.BTN_SEGMENT_PAID, BroadcastCb(action="paid"))],
        [_btn(texts.BTN_SEGMENT_UNPAID, BroadcastCb(action="unpaid"))],
        [_btn(texts.BTN_CANCEL, BroadcastCb(action="cancel"))],
    )


def broadcast_cancel_kb() -> InlineKeyboardMarkup:
    return _kb([_btn(texts.BTN_CANCEL, BroadcastCb(action="cancel"))])


def broadcast_stop_kb() -> InlineKeyboardMarkup:
    return _kb([_btn(texts.BTN_BROADCAST_STOP, BroadcastCb(action="stop"))])


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn(texts.BTN_BROADCAST_SEND, BroadcastCb(action="send"))],
        [_btn(texts.BTN_CANCEL, BroadcastCb(action="cancel"))],
    )
