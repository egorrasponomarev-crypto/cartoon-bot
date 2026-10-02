"""Состояния «бот ждёт сообщение»: результат, вопрос автору, действия админа."""
from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    waiting_result = State()  # ученик нажал «Отправить результат»
    waiting_question = State()  # ученик нажал «Написать автору»


class AdminStates(StatesGroup):
    fileid = State()  # /fileid — ждём фото или видео
    broadcast_segment = State()  # /broadcast — выбор сегмента
    broadcast_content = State()  # /broadcast — ждём сообщение для рассылки
    broadcast_confirm = State()  # /broadcast — подтверждение
