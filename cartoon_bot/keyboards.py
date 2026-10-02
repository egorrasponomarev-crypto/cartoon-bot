"""Кнопки бота. Названия кнопок берутся из texts.py."""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

import texts
from config import settings
from utils import fmt

# Куда ведёт кнопка «Нужна помощь». При запуске бот подставляет сюда ссылку на чат с админом
# (если HELP_URL в .env не задан). Пусто — кнопка открывает вопрос автору внутри бота.
_help_url: str = settings.help_url


def set_help_url(url: str) -> None:
    global _help_url
    _help_url = url

# ---------------------------------------------------------------- данные кнопок


class StepCb(CallbackData, prefix="st"):
    action: str  # open (step=0 — приветствие) | done | finish | file (скачать файл шага) | result | stuck
    step: int


class NavCb(CallbackData, prefix="nav"):
    action: str  # offer | program | faq | pay | paid_d | paid_f | ask | cancel | mysteps
    step: int = 0  # для offer/program/faq: с какого шага открыли (туда ведёт «Назад»); 0 — без «Назад»

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
    action: str  # all | paid | unpaid | send | cancel


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
    """Под приветствием — всегда «Начать шаг 1» (бот не предлагает «продолжить с места»)."""
    return _kb([_btn(texts.BTN_START_STEP1, StepCb(action="open", step=1))])


def help_button() -> InlineKeyboardButton:
    """«Нужна помощь»: ссылка на чат с админом, а если ссылки нет — вопрос автору внутри бота."""
    if _help_url:
        return InlineKeyboardButton(text=texts.BTN_HELP, url=_help_url)
    return _btn(texts.BTN_HELP, NavCb(action="ask"))


def help_kb() -> InlineKeyboardMarkup:
    return _kb([help_button()])


def step_kb(step: int, discount_active: bool) -> InlineKeyboardMarkup:
    if step < 5:
        main = _btn(texts.BTN_NEXT_STEP.get(step, texts.BTN_DONE), StepCb(action="done", step=step))
    else:
        label = texts.BTN_GET_DISCOUNT if discount_active else texts.BTN_GET_COURSE
        main = _btn(label, StepCb(action="finish", step=step))
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
        [_btn(texts.BTN_WHATS_INSIDE, NavCb(action="offer", step=step))] if step >= 3 else [],
    )


def steps_menu_kb() -> InlineKeyboardMarkup:
    """Список шагов (кнопка меню «📚 Мои шаги»): можно перейти к любому."""
    rows = [[_btn(texts.BTN_STEPS_MENU[n], StepCb(action="open", step=n))] for n in sorted(texts.BTN_STEPS_MENU)]
    return _kb(*rows, [_btn(texts.BTN_FULL_COURSE, NavCb(action="offer"))])


def stuck_kb() -> InlineKeyboardMarkup:
    return _kb([_btn(texts.BTN_WRITE_AUTHOR, NavCb(action="ask"))])


def cancel_kb() -> InlineKeyboardMarkup:
    return _kb([_btn(texts.BTN_CANCEL, NavCb(action="cancel"))])


def offer_kb(back_step: int = 0) -> InlineKeyboardMarkup:
    """Оффер. back_step — шаг, с которого его открыли: туда ведёт «Назад» (0 — без кнопки «Назад»)."""
    return _kb(
        [_btn(texts.BTN_PAY, NavCb(action="pay"))],
        [_btn(texts.BTN_PROGRAM, NavCb(action="program", step=back_step))],
        [_btn(texts.BTN_FAQ, NavCb(action="faq", step=back_step))],
        [_btn(texts.BTN_ASK_AUTHOR, NavCb(action="ask"))],
        [_btn(texts.BTN_BACK, StepCb(action="open", step=back_step))] if back_step else [],
    )


def back_kb(step: int) -> InlineKeyboardMarkup | None:
    """Одна кнопка «👈 Назад» к шагу step (0 — к приветствию); None, если возвращаться некуда."""
    return _kb([_btn(texts.BTN_BACK, StepCb(action="open", step=step))]) if step else None


def back_to_offer_kb(back_step: int = 0) -> InlineKeyboardMarkup:
    """Под «Программой» и «Частыми вопросами»: «Назад» возвращает к офферу."""
    return _kb(
        [_btn(texts.BTN_PAY, NavCb(action="pay"))],
        [_btn(texts.BTN_BACK, NavCb(action="offer", step=back_step))],
    )


def remind_step_kb(step: int, with_help: bool) -> InlineKeyboardMarkup:
    return _kb(
        [_btn(fmt(texts.BTN_CONTINUE_STEP, step=step), StepCb(action="open", step=step))],
        [help_button()] if with_help else [],
    )


def offer_reminder_kb(with_author: bool = False) -> InlineKeyboardMarkup:
    return _kb(
        [_btn(texts.BTN_PAY, NavCb(action="pay"))],
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


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn(texts.BTN_BROADCAST_SEND, BroadcastCb(action="send"))],
        [_btn(texts.BTN_CANCEL, BroadcastCb(action="cancel"))],
    )
