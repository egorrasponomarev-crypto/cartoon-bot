"""Логика воронки: регистрация, шаги, скидка, оффер, напоминания, доступ в канал.

Этот модуль используют и обработчики кнопок, и планировщик напоминаний.
"""
import asyncio
import contextlib
import hashlib
import io
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
    User,
)

import db
import history
import keyboards as kb
import texts
from config import BASE_DIR, settings
from utils import (
    CAPTION_LIMIT,
    MEDIA_KINDS,
    discount_price_text,
    esc,
    fmt,
    format_hours,
    format_timer,
    full_price_text,
    shift_quiet,
    shift_quiet_back,
    video_meta,
    visible_length,
)

logger = logging.getLogger(__name__)

STEP_JOBS = ("step_24", "step_72")
LAST_STEP = 5


# ================================================================ безопасная отправка

# Ошибки Telegram, о которых админ уже знает (чтобы не присылать одно и то же много раз)
_reported_errors: set[str] = set()


async def call_safe(
    bot: Bot,
    user_id: int,
    make_call: Callable[[], Awaitable[Any]],
    preview: str = "",
    media_name: str | None = None,
    raise_bad_request: bool = False,
) -> Any:
    """Вызывает метод Telegram для ученика.

    Если ученик заблокировал бота — помечает это в базе и больше ему не пишет.
    Если Telegram просит подождать — ждёт и пробует снова.
    Если Telegram не принял сообщение (обычно ошибка в texts.py) — один раз сообщает админу.
    media_name — это отправка картинки/видео (тогда админу придёт подсказка про файл, а не про текст).
    raise_bad_request — не сообщать админу, а пробросить ошибку: вызывающий сам решит, что делать.
    Возвращает результат или None, если отправить не получилось.
    Сбои связи и лимит Telegram (после трёх попыток) пробрасываются наружу —
    планировщик повторит попытку позже.
    """
    last_flood: TelegramRetryAfter | None = None
    for attempt in range(3):
        try:
            return await make_call()
        except TelegramRetryAfter as exc:
            last_flood = exc
            if attempt < 2:
                await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            logger.info("Пользователь %s заблокировал бота — больше не пишем ему", user_id)
            await db.set_blocked(user_id, True)
            return None
        except TelegramBadRequest as exc:
            error = str(exc)
            if "chat not found" in error.lower():
                await db.set_blocked(user_id, True)
                return None
            if raise_bad_request:
                raise
            logger.warning("Telegram не принял сообщение для %s: %s", user_id, error)
            if media_name is not None:
                await _report_media_error(bot, error, media_name)
            else:
                await _report_send_error(bot, error, preview)
            return None
    raise last_flood


async def _report_send_error(bot: Bot, error: str, preview: str) -> None:
    key = error[:200]
    if key in _reported_errors:
        return
    _reported_errors.add(key)
    await notify_admin(bot, fmt(texts.ADMIN_SEND_ERROR, error=esc(error), preview=esc(preview[:80]) or "—"))


async def _report_media_error(bot: Bot, error: str, media_name: str) -> None:
    key = f"media:{media_name}:{error[:200]}"
    if key in _reported_errors:
        return
    _reported_errors.add(key)
    await notify_admin(bot, fmt(texts.ADMIN_MEDIA_ERROR, file=esc(media_name), error=esc(error)))


async def answer_callback(callback: Any, text: str | None = None, show_alert: bool = False) -> None:
    """Убирает «часики» с нажатой кнопки (и показывает окошко с text, если он задан).

    Если Telegram не принял ответ (кнопку нажали, пока бот перезапускался, — такой ответ уже
    «слишком старый», — или пропала связь), не страшно: само действие кнопки всё равно выполняем.
    """
    with contextlib.suppress(TelegramAPIError):
        await callback.answer(text, show_alert=show_alert)


def above_text_preview(url: str | None) -> dict:
    """Картинка по ссылке крупно НАД текстом сообщения (как в постах каналов). Пусто — обычное превью."""
    if not url:
        return {}
    return {"link_preview_options": LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=True)}


async def send_text(bot: Bot, user_id: int, text: str, reply_markup: Any = None) -> Message | None:
    return await call_safe(
        bot, user_id, lambda: bot.send_message(user_id, text, reply_markup=reply_markup), preview=text
    )


async def admin_call(make_call: Callable[[], Awaitable[Any]]) -> Any:
    """Вызов Telegram в сторону админа: ждёт, если Telegram просит подождать, и никогда не падает.

    При сбое связи пробует ещё пару раз: иначе одна секундная заминка — и админ не узнал бы об оплате.
    """
    network_failures = 0
    for _ in range(5):
        try:
            return await make_call()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError) as exc:
            network_failures += 1
            if network_failures >= 3:
                logger.error("Не удалось написать админу: нет связи с Telegram (%s)", exc)
                return None
            await asyncio.sleep(2 * network_failures)
        except Exception as exc:  # noqa: BLE001 — сообщение админу не должно ронять обработку ученика
            logger.error("Не удалось написать админу (%s). Админ должен нажать /start в своём боте.", exc)
            return None
    return None


async def notify_admin(
    bot: Bot,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    about_user: int | None = None,
) -> Message | None:
    """Пишет админу. about_user — ученик, которому уйдёт ответ, если админ ответит на это сообщение."""
    if not settings.admin_id:
        logger.warning("ADMIN_ID не указан в .env — сообщение админу не отправлено")
        return None
    message = await admin_call(lambda: bot.send_message(settings.admin_id, text, reply_markup=reply_markup))
    if message is not None and about_user is not None:
        await db.link_admin_message(message.message_id, about_user)
    return message


# ================================================================ ученик


def user_card(user: dict | None, tg_user: User | None = None) -> str:
    """Короткое описание ученика для сообщений админу."""
    user = user or {}
    user_id = user.get("user_id") or (tg_user.id if tg_user else 0)
    first_name = user.get("first_name") or (tg_user.first_name if tg_user else "") or "—"
    username = user.get("username") or (tg_user.username if tg_user else None)
    step = user.get("current_step") or 1
    if user.get("finished_at"):
        step = fmt(texts.ADMIN_STEP_FINISHED, step=LAST_STEP)
    return fmt(
        texts.ADMIN_USER_CARD,
        name=f'<a href="tg://user?id={user_id}">{esc(first_name)}</a>',
        username=f"@{esc(username)}" if username else texts.ADMIN_NO_USERNAME,
        user_id=user_id,
        step=step,
        source=esc(user.get("source") or texts.ADMIN_NO_SOURCE),
    )


def discount_left(user: dict, ts: int | None = None) -> int:
    """Сколько секунд осталось до конца скидки (0 — скидки нет)."""
    until = user.get("discount_until") or 0
    return max(0, until - (ts if ts is not None else db.now()))


def discount_active(user: dict, ts: int | None = None) -> bool:
    return discount_left(user, ts) > 0


def min_last_call_left() -> int:
    """Если до конца скидки осталось меньше этого, «последний звонок» уже не шлём."""
    return settings.hours(0.5)


async def ensure_user(tg_user: User) -> dict:
    """Находит ученика в базе или регистрирует нового (и запускает его личный таймер скидки)."""
    user = await db.get_user(tg_user.id)
    if user is None:
        ts = db.now()
        discount_until = ts + settings.hours(settings.discount_hours)
        if await db.create_user(tg_user.id, tg_user.first_name, tg_user.username, discount_until):
            await history.track(tg_user.id, "joined")
            await schedule_discount_jobs(tg_user.id, discount_until)
            await schedule_step_reminders(tg_user.id, 1, ts)
        return await db.get_user(tg_user.id)
    await db.touch_user(tg_user.id, tg_user.first_name, tg_user.username)
    user.update(first_name=tg_user.first_name, username=tg_user.username, last_activity_at=db.now(), blocked=0)
    return user


# ================================================================ напоминания


def apart_from_discount_end(user: dict | None, from_ts: int, run_at: int) -> int:
    """Не присылаем напоминание в ту же минуту, что и «Скидка закончилась» — разносим на 2 часа."""
    until = (user or {}).get("discount_until") or 0
    ended_at = shift_quiet(until) if until else 0
    if ended_at > from_ts and abs(run_at - ended_at) < settings.hours(1):
        return shift_quiet(ended_at + settings.hours(2))
    return run_at


async def schedule_step_reminders(user_id: int, step: int, from_ts: int) -> None:
    """Напоминания «как там шаг N?» через 24 и 72 часа (старые напоминания о шагах заменяются)."""
    run_24 = shift_quiet(from_ts + settings.hours(settings.step_reminder_1_hours))
    run_72 = shift_quiet(from_ts + settings.hours(settings.step_reminder_2_hours))
    run_24 = apart_from_discount_end(await db.get_user(user_id), from_ts, run_24)
    await db.delete_jobs(user_id, STEP_JOBS)
    await db.add_job(user_id, "step_24", run_24, step)
    await db.add_job(user_id, "step_72", run_72, step)


async def schedule_discount_jobs(user_id: int, discount_until: int) -> None:
    """«Скоро конец скидки» и «Скидка закончилась»."""
    ts = db.now()
    await db.delete_jobs(user_id, ("offer_last_call", "offer_ended"))
    last_call = discount_until - settings.hours(settings.last_call_hours)
    if last_call > ts:
        run_at = shift_quiet(last_call)
        if discount_until - run_at < min_last_call_left():
            # Утром скидка уже закончится — тогда предупреждаем накануне вечером
            evening = shift_quiet_back(last_call)
            if evening > ts:
                run_at = evening
        if discount_until - run_at >= min_last_call_left():
            await db.add_job(user_id, "offer_last_call", run_at)
    if discount_until > ts:
        await db.add_job(user_id, "offer_ended", shift_quiet(discount_until))


# ================================================================ шаги


# Файл из папки бота → его file_id в Telegram после первой отправки (чтобы не загружать файл каждый раз)
_uploaded_file_ids: dict[str, str] = {}


def media_list(value: Any) -> list[dict]:
    """Медиа из texts.py: список записей; одна запись без квадратных скобок тоже подойдёт."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, dict)]
    return []


def media_path(item: dict) -> Path | None:
    """Путь к файлу из записи вида {"type": "photo", "file": "step0.png"} (относительно папки бота)."""
    name = item.get("file")
    if not name:
        return None
    path = Path(name)
    return path if path.is_absolute() else BASE_DIR / path


def _file_id_of(message: Any, kind: str) -> str | None:
    if kind == "photo":
        photos = getattr(message, "photo", None)
        return photos[-1].file_id if photos else None
    media = getattr(message, kind, None)
    return getattr(media, "file_id", None)


async def send_media(
    bot: Bot,
    user_id: int,
    item: Any,
    caption: str | None = None,
    reply_markup: Any = None,
) -> Message | None:
    """Отправляет фото/видео/GIF/файл: по file_id или из файла в папке бота. Возвращает сообщение или None."""
    if not isinstance(item, dict):
        logger.warning("Медиа: неправильная запись %r — нужен вид {'type': ..., 'file': ...}", item)
        return None
    kind = item.get("type")
    if kind not in MEDIA_KINDS:
        logger.warning("Медиа: неизвестный тип %r (можно: %s)", kind, ", ".join(MEDIA_KINDS))
        return None
    source: Any = item.get("file_id")
    cache_key = None
    meta: dict[str, int] = {}
    if not source:
        path = media_path(item)
        if path is None:
            logger.warning("Медиа: в записи %r нет ни file, ни file_id", item)
            return None
        # file_id фото нельзя отправить как документ (и наоборот), поэтому тип — часть ключа
        cache_key = f"{kind}:{path}"
        source = _uploaded_file_ids.get(cache_key)
        if source is None:
            if not path.is_file():
                logger.warning("Медиа: файл %s не найден в папке бота", path.name)
                return None
            # Telegram показывает имя только у документов; для фото/видео шлём латинское имя —
            # так загрузка не зависит от кириллицы и пробелов в названии файла
            upload_name = item.get("filename") or (path.name if kind == "document" else f"{kind}{path.suffix.lower()}")
            source = FSInputFile(path, filename=upload_name)
            if kind in ("video", "animation"):
                # ширина, высота и длительность: без них Telegram может растянуть видео в квадрат
                meta = video_meta(path)
    sender = getattr(bot, f"send_{kind}")
    extra: dict[str, Any] = {"caption": caption} if caption else {}
    if kind == "video":
        extra["supports_streaming"] = True  # видео начинает играть сразу, не дожидаясь загрузки целиком
    extra.update(meta)
    if reply_markup is not None:
        extra["reply_markup"] = reply_markup
    media_name = str(item.get("file") or item.get("file_id") or kind)
    try:
        sent = await call_safe(bot, user_id, lambda: sender(user_id, source, **extra), media_name=media_name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — картинка не обязательна: текст шага/приветствия всё равно уйдёт
        error = str(exc) or exc.__class__.__name__
        logger.warning("Не удалось отправить медиа %s ученику %s: %s", media_name, user_id, error)
        await _report_media_error(bot, error, media_name)
        return None
    if sent is not None and cache_key is not None and cache_key not in _uploaded_file_ids:
        file_id = _file_id_of(sent, kind)
        if file_id:
            _uploaded_file_ids[cache_key] = file_id
    return sent


# Альбомом Telegram показывает от 2 до 10 фото и видео
ALBUM_KINDS = ("photo", "video")
ALBUM_MAX = 10


def is_album(media: list[dict]) -> bool:
    """Несколько фото/видео подряд — одним альбомом, а не отдельными сообщениями."""
    return 2 <= len(media) <= ALBUM_MAX and all(item.get("type") in ALBUM_KINDS for item in media)


async def send_album(bot: Bot, user_id: int, items: list[dict]) -> list[Message] | None:
    """Отправляет фото/видео одним альбомом.

    Возвращает сообщения альбома. None — альбом не собрался или Telegram его не принял
    (тогда шлём по одному: какие получится). [] — подвела связь: по одному слать не стоит.
    """
    group: list[Any] = []
    cache_keys: list[str | None] = []
    for number, item in enumerate(items, 1):
        kind = item.get("type")
        source: Any = item.get("file_id")
        cache_key = None
        meta: dict[str, int] = {}
        if not source:
            path = media_path(item)
            if path is None:
                return None
            cache_key = f"{kind}:{path}"
            source = _uploaded_file_ids.get(cache_key)
            if source is None:
                if not path.is_file():
                    logger.warning("Медиа: файл %s не найден в папке бота", path.name)
                    return None
                source = FSInputFile(path, filename=f"{kind}{number}{path.suffix.lower()}")
                if kind == "video":
                    meta = video_meta(path)  # без размеров Telegram может растянуть видео в квадрат
        if kind == "video":
            group.append(InputMediaVideo(media=source, supports_streaming=True, **meta))
        else:
            group.append(InputMediaPhoto(media=source))
        cache_keys.append(cache_key)
    media_name = ", ".join(str(item.get("file") or item.get("file_id")) for item in items)
    try:
        sent = await call_safe(
            bot, user_id, lambda: bot.send_media_group(user_id, media=group), media_name=media_name
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — альбом не обязателен: текст шага всё равно уйдёт
        error = str(exc) or exc.__class__.__name__
        logger.warning("Не удалось отправить альбом %s ученику %s: %s", media_name, user_id, error)
        await _report_media_error(bot, error, media_name)
        return []
    if not sent:
        return None
    for message, item, cache_key in zip(sent, items, cache_keys):
        if cache_key is not None and cache_key not in _uploaded_file_ids:
            file_id = _file_id_of(message, str(item.get("type")))
            if file_id:
                _uploaded_file_ids[cache_key] = file_id
    return list(sent)


# ================================================================ «экраны»: новый экран заменяет предыдущий


@dataclass
class ScreenPart:
    """Одно сообщение экрана: текст или картинка/видео (тогда text — подпись к ней).

    preview_url — картинка по ссылке крупно над текстом (для текстов длиннее подписи к фото).
    fallback — что отправить вместо этого сообщения, если Telegram не примет картинку по ссылке.
    album — несколько фото/видео одним альбомом (без текста и кнопок: под альбомом Telegram кнопки не ставит).
    """

    text: str | None = None
    media: dict | None = None
    reply_markup: Any = None
    preview_url: str | None = None
    fallback: list["ScreenPart"] | None = None
    album: list[dict] | None = None


def media_parts(media: list[dict]) -> list[ScreenPart]:
    """Медиа перед текстом: несколько фото/видео — одним альбомом, остальное — по одному."""
    if is_album(media):
        return [ScreenPart(album=media)]
    return [ScreenPart(media=item) for item in media]


# Удалять свои сообщения Telegram разрешает только первые 48 часов (берём с запасом)
DELETE_LIMIT_SECONDS = 47 * 3600


async def remove_buttons(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    """Убирает кнопки у сообщений, которые уже нельзя удалить, — чтобы в чате не было двух рабочих экранов."""
    for message_id in dict.fromkeys(message_ids):
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)


def pressed_id(callback: Any) -> int | None:
    """Сообщение, на котором нажали кнопку: его заменит новый экран."""
    message = getattr(callback, "message", None)
    return getattr(message, "message_id", None) if message is not None else None


async def delete_messages(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    """Удаляет сообщения в личке (свои и ученика). Старше 48 часов Telegram удалить не даст — пропускаем."""
    ids = [message_id for message_id in dict.fromkeys(message_ids) if message_id]
    if not ids:
        return
    try:
        await bot.delete_messages(chat_id, ids[:100])
    except Exception:  # noqa: BLE001 — тогда по одному: что получится, то удалится
        for message_id in ids:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:  # noqa: BLE001 — старше 48 часов удалить нельзя, но кнопки убрать можно
                with contextlib.suppress(Exception):
                    await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)


# Ошибка Telegram, если он не принял картинку по ссылке над текстом (None — пока всё работает).
# После первой такой ошибки бот до перезапуска шлёт картинку отдельным сообщением.
_preview_error: str | None = None


async def _preview_failed(bot: Bot, url: str, error: str) -> None:
    global _preview_error
    if _preview_error is not None:
        return
    _preview_error = error
    logger.warning("Telegram не принял картинку над текстом (%s): %s — дальше шлю её отдельно", url, error)
    await notify_admin(bot, fmt(texts.ADMIN_PREVIEW_FAILED, url=esc(url), error=esc(error)))


async def _send_part(bot: Bot, user_id: int, part: ScreenPart) -> list[tuple[int, str]]:
    """Отправляет часть экрана. Возвращает отправленные сообщения: [(message_id, тип), ...]."""
    if part.album:
        messages = await send_album(bot, user_id, part.album)
        if messages is not None:
            return [(message.message_id, str(item.get("type"))) for message, item in zip(messages, part.album)]
        # альбом не ушёл — фото и видео по одному, какие получится
        singles: list[tuple[int, str]] = []
        for item in part.album:
            singles += await _send_part(bot, user_id, ScreenPart(media=item))
        return singles
    if part.media is not None:
        message = await send_media(bot, user_id, part.media, caption=part.text, reply_markup=part.reply_markup)
        if message is not None:
            return [(message.message_id, str(part.media.get("type")))]
        if not part.text:
            return []
        # картинку отправить не получилось — подпись к ней (и кнопки) уйдут обычным сообщением
    elif part.preview_url:
        if _preview_error is None:
            preview = above_text_preview(part.preview_url)
            try:
                message = await call_safe(
                    bot,
                    user_id,
                    lambda: bot.send_message(user_id, part.text, reply_markup=part.reply_markup, **preview),
                    preview=part.text,
                    raise_bad_request=True,
                )
                return [(message.message_id, "text")] if message is not None else []
            except TelegramBadRequest as exc:
                if "WEBPAGE" not in str(exc).upper():
                    # дело не в картинке по ссылке, а в самом тексте или кнопках: прежний экран остаётся
                    logger.warning("Telegram не принял сообщение для %s: %s", user_id, exc)
                    await _report_send_error(bot, str(exc), part.text or "")
                    return []
                await _preview_failed(bot, part.preview_url, str(exc))
        # картинку по ссылке Telegram не принял — картинка отдельным сообщением, потом текст с кнопками
        sent: list[tuple[int, str]] = []
        try:
            for extra in part.fallback or [ScreenPart(text=part.text, reply_markup=part.reply_markup)]:
                sent += await _send_part(bot, user_id, extra)
        except Exception:
            # сбой посреди отправки (например, пропала связь): не оставляем в чате картинку без текста
            await delete_messages(bot, user_id, [message_id for message_id, _ in sent])
            raise
        return sent
    message = await send_text(bot, user_id, part.text, part.reply_markup)
    return [(message.message_id, "text")] if message is not None else []


async def show_screen(
    bot: Bot, user_id: int, parts: list[ScreenPart], pressed_message_id: int | None = None
) -> bool:
    """Показывает новый «экран» (шаг, приветствие, оффер…) вместо предыдущего, чтобы не засорять чат.

    Если кнопку нажали на самом экране, он из одного текстового сообщения и новый — тоже текст,
    сообщение просто меняется на месте. Иначе новый экран отправляется, а старый удаляется.
    pressed_message_id — сообщение с нажатой кнопкой (например, напоминание): его тоже убираем.
    """
    old_items = await db.get_screen_items(user_id)
    old = [(message_id, kind) for message_id, kind, _ in old_items]
    if (
        pressed_message_id is not None
        and len(parts) == 1
        and parts[0].media is None
        and parts[0].album is None
        and old == [(pressed_message_id, "text")]
    ):
        for attempt in range(2):
            try:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=pressed_message_id,
                    text=parts[0].text,
                    reply_markup=parts[0].reply_markup,
                    **above_text_preview(parts[0].preview_url),
                )
                return True
            except TelegramRetryAfter as exc:
                # Telegram просит подождать (ученик часто нажимает кнопки): ждём и пробуем ещё раз,
                # а если опять «подожди» — отправляем экран заново (там свои повторы)
                if attempt:
                    break
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return True
                logger.info("Не получилось изменить сообщение %s (%s) — отправляю заново", pressed_message_id, exc)
                break
    sent: list[tuple[int, str]] = []
    try:
        for part in parts:
            sent += await _send_part(bot, user_id, part)
    except Exception:
        # сбой посреди экрана (например, пропала связь): убираем то, что успело уйти; прежний экран остаётся
        await delete_messages(bot, user_id, [message_id for message_id, _ in sent])
        raise
    if not sent:
        return False
    await db.set_screen(user_id, sent)
    stale = [message_id for message_id, _ in old]
    if pressed_message_id is not None:
        stale.append(pressed_message_id)
    new_ids = {message_id for message_id, _ in sent}
    # Telegram не даёт удалять сообщения старше 48 часов — у таких просто убираем кнопки
    too_old = {
        message_id
        for message_id, _, sent_at in old_items
        if sent_at and db.now() - sent_at > DELETE_LIMIT_SECONDS
    }
    await delete_messages(bot, user_id, [m for m in stale if m not in new_ids and m not in too_old])
    await remove_buttons(bot, user_id, [m for m in stale if m not in new_ids and m in too_old])
    return True


async def send_to_screen(bot: Bot, user_id: int, text: str, reply_markup: Any = None) -> Message | None:
    """Короткое сообщение, которое уберётся вместе с текущим экраном при следующем переходе."""
    message = await send_text(bot, user_id, text, reply_markup)
    if message is not None:
        await db.append_screen(user_id, message.message_id, "text")
    return message


async def remove_old_menu(bot: Bot, user_id: int, force: bool = False) -> None:
    """Нижнего меню в боте больше нет. У кого оно осталось с прошлой версии — убираем клавиатуру
    и подсказку «📌 Меню — на кнопках внизу экрана». force=True — человек точно нажал старую кнопку меню.

    Клавиатуру убирает только сообщение с ReplyKeyboardRemove, и его оставляем в чате: если его удалить,
    Telegram на компьютере и в браузере может снова показать старое меню из истории чата.
    """
    user = await db.get_user(user_id) or {}
    old_id = user.get("menu_msg_id")
    if not (old_id or user.get("old_menu") or force):
        return
    if await send_text(bot, user_id, texts.MENU_REMOVED, kb.remove_menu()) is None:
        return  # не дошло — попробуем при следующем действии ученика
    await db.update_user(user_id, menu_msg_id=None, old_menu=0)
    if old_id:
        await delete_messages(bot, user_id, [old_id])


def greeting_screen(first_name: str) -> list[ScreenPart]:
    """Приветствие. Если к нему одна картинка и текст влезает в подпись — одним сообщением."""
    text = fmt(texts.GREETING, name=esc(first_name or ""))
    media = media_list(getattr(texts, "GREETING_MEDIA", None))
    markup = kb.start_kb()
    if len(media) == 1 and visible_length(text) <= CAPTION_LIMIT:
        return [ScreenPart(text=text, media=media[0], reply_markup=markup)]
    return media_parts(media) + [ScreenPart(text=text, reply_markup=markup)]


async def show_greeting(bot: Bot, user_id: int, first_name: str, pressed_message_id: int | None = None) -> bool:
    return await show_screen(bot, user_id, greeting_screen(first_name), pressed_message_id)


def step_parts(user: dict, step: int) -> list[str]:
    """Сообщения шага. В texts.py шаг — это один текст или список текстов (несколько сообщений подряд)."""
    value = texts.STEP_TEXTS[step]
    parts = [value] if isinstance(value, str) else [part for part in value if part]
    hint = texts.STEP_DISCOUNT_HINTS.get(step)
    if hint and discount_active(user):
        parts[-1] = f"{parts[-1]} {hint}"
    return parts


def step_text(user: dict, step: int) -> str:
    """Последнее сообщение шага — то, под которым стоят кнопки."""
    return step_parts(user, step)[-1]


# файл картинки → её адрес в интернете (картинка уже лежит в папке веб-сервера); None — не подошла
_published_urls: dict[str, str | None] = {}

# Картинку над текстом Telegram скачивает сам и берёт только до 5 МБ. Всё, что больше 1 МБ,
# бот пересохраняет в JPG поменьше (до 1600 точек по большей стороне) — так она и грузится быстрее.
WEB_IMAGE_LIMIT = 5 * 1024 * 1024
WEB_IMAGE_SHRINK_FROM = 1024 * 1024
WEB_IMAGE_MAX_SIDE = 1600


def _web_image(data: bytes, suffix: str) -> tuple[bytes, str]:
    """Большую картинку уменьшает до JPG. Без библиотеки Pillow или при ошибке — возвращает как есть."""
    if len(data) <= WEB_IMAGE_SHRINK_FROM:
        return data, suffix
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)  # фото с телефона — правильной стороной вверх
            if image.mode in ("RGBA", "LA", "P"):
                # в JPG нет прозрачности: прозрачный фон делаем белым
                image = image.convert("RGBA")
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            image = image.convert("RGB")
            image.thumbnail((WEB_IMAGE_MAX_SIDE, WEB_IMAGE_MAX_SIDE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue(), ".jpg"
    except Exception as exc:  # noqa: BLE001 — нет Pillow или необычный файл: публикуем как есть
        logger.warning("Не удалось уменьшить картинку (%s) — публикую как есть", exc)
        return data, suffix


def public_media_url(item: dict) -> str | None:
    """Кладёт картинку в папку веб-сервера (MEDIA_WEB_DIR) и возвращает ссылку на неё.

    Нужна, чтобы показать картинку крупно над длинным текстом. Имя файла — по содержимому,
    поэтому новая картинка с тем же именем получит новую ссылку (Telegram не покажет старую из кэша).
    None — если папка не настроена, файла нет или он больше 5 МБ: тогда картинка придёт отдельным сообщением.
    """
    if not (settings.media_web_dir and settings.media_base_url) or item.get("type") != "photo":
        return None
    if _preview_error is not None:  # Telegram уже не принял такую картинку — шлём отдельным сообщением
        return None
    path = media_path(item)
    if path is None or not path.is_file():
        return None
    key = str(path)
    if key in _published_urls:
        return _published_urls[key]
    try:
        original = path.read_bytes()
        data, suffix = _web_image(original, path.suffix.lower())
        if len(data) > WEB_IMAGE_LIMIT:
            logger.warning(
                "Картинка %s весит %.1f МБ — над текстом Telegram показывает только до 5 МБ, "
                "поэтому она придёт отдельным сообщением",
                path.name,
                len(data) / 1024 / 1024,
            )
            _published_urls[key] = None
            return None
        name = hashlib.sha1(original).hexdigest()[:16] + suffix
        folder = Path(settings.media_web_dir)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        # недописанный файл (бота перезапустили во время записи) не считаем готовым
        if not (target.is_file() and target.stat().st_size == len(data)):
            temp = folder / f".{name}.{os.getpid()}.tmp"
            temp.write_bytes(data)
            temp.chmod(0o644)
            os.replace(temp, target)
    except OSError as exc:
        logger.warning("Не удалось положить картинку %s в %s: %s", path.name, settings.media_web_dir, exc)
        return None
    url = f"{settings.media_base_url}/{name}"
    _published_urls[key] = url
    return url


def publish_step_images() -> None:
    """При запуске заранее кладём картинки шагов в веб-папку, чтобы первый ученик не ждал."""
    for items in (getattr(texts, "STEP_MEDIA", None) or {}).values():
        media = media_list(items)
        if len(media) == 1:
            public_media_url(media[0])


def step_screen(user: dict, step: int) -> list[ScreenPart]:
    """Сообщения шага. Кнопки — под последним.

    Шаг с одной картинкой и одним текстом приходит ОДНИМ сообщением: фото с подписью,
    а если текст длиннее подписи (1024 символа) — картинка крупно над текстом.
    """
    media = media_list((getattr(texts, "STEP_MEDIA", None) or {}).get(step))
    messages = step_parts(user, step)
    markup = kb.step_kb(step)
    if len(media) == 1 and len(messages) == 1 and media[0].get("type") in ("photo", "video", "animation"):
        text = messages[0]
        if visible_length(text) <= CAPTION_LIMIT:
            # фото/видео с текстом-подписью — одно сообщение
            return [ScreenPart(text=text, media=media[0], reply_markup=markup)]
        url = public_media_url(media[0])  # только для фото: картинка над длинным текстом
        if url:
            fallback = [ScreenPart(media=media[0]), ScreenPart(text=text, reply_markup=markup)]
            return [ScreenPart(text=text, reply_markup=markup, preview_url=url, fallback=fallback)]
    parts = media_parts(media)
    parts += [ScreenPart(text=text) for text in messages[:-1]]
    parts.append(ScreenPart(text=messages[-1], reply_markup=markup))
    return parts


async def _note_progress(user: dict, step: int) -> None:
    """Запоминаем самый дальний шаг — для напоминаний «как там шаг N?» и статистики.

    Сам ученик ходит по шагам свободно, вперёд и назад: бот не заставляет «продолжать с места».
    """
    current = user["current_step"]
    if step < current or (step == current and user["step_opened_at"]):
        return
    ts = db.now()
    user_id = user["user_id"]
    await db.update_user(
        user_id, current_step=step, step_opened_at=ts, max_step_opened=max(user["max_step_opened"], step)
    )
    if not user["paid_at"] and not user["finished_at"]:
        await schedule_step_reminders(user_id, step, ts)


async def open_step(bot: Bot, user_id: int, step: int, pressed_message_id: int | None = None) -> None:
    """Показывает шаг 1–5 вместо текущего экрана (шаг 0 — приветствие)."""
    user = await db.get_user(user_id)
    if user is None:
        return
    if step <= 0:
        if await show_greeting(bot, user_id, user["first_name"], pressed_message_id):
            await history.track(user_id, "greeting")
        return
    step = min(step, LAST_STEP)
    await _note_progress(user, step)
    if await show_screen(bot, user_id, step_screen(user, step), pressed_message_id):
        await history.track(user_id, "step", step)


async def complete_step(bot: Bot, user_id: int, step: int, pressed_message_id: int | None = None) -> None:
    """Кнопка «👉 Шаг N+1…» под шагом step."""
    await open_step(bot, user_id, min(step + 1, LAST_STEP), pressed_message_id)


async def finish_course(bot: Bot, user_id: int, pressed_message_id: int | None = None) -> None:
    """Кнопка «🔥 Что дальше?» на шаге 5: практикум пройден, дальше — экраны про полный курс."""
    user = await db.get_user(user_id)
    if user is None:
        return
    await history.track(user_id, "finish")
    if not user["finished_at"]:
        await _note_progress(user, LAST_STEP)
        await db.update_user(user_id, finished_at=db.now())
        await db.delete_jobs(user_id, STEP_JOBS)
    if await show_sales_page(bot, user_id, "pitch", pressed_message_id):
        await _schedule_offer_reminder(user)


async def send_step_download(bot: Bot, user_id: int, step: int) -> bool:
    """Кнопка «📄 Забрать…» под шагом: присылает файл (например, банк промптов). False — не получилось.

    Файл становится частью экрана: при переходе на другой шаг он уберётся, а скачать его снова — в один тап.
    """
    download = (getattr(texts, "STEP_DOWNLOADS", None) or {}).get(step)
    if not isinstance(download, dict) or not download.get("file"):
        return False
    item = {"type": "document", "file": download["file"], "filename": download.get("filename")}
    message = await send_media(bot, user_id, item, caption=download.get("caption"))
    if message is None:
        return False
    await db.append_screen(user_id, message.message_id, "document")
    await history.track(user_id, "file", step)
    return True


async def show_steps_menu(bot: Bot, user_id: int, pressed_message_id: int | None = None) -> None:
    """Кнопка меню «📚 Мои шаги»: список всех шагов, можно перейти к любому."""
    user = await db.get_user(user_id)
    if user is None:
        return
    text = texts.ALL_STEPS_DONE if user["finished_at"] else texts.STEPS_MENU
    if await show_screen(bot, user_id, [ScreenPart(text=text, reply_markup=kb.steps_menu_kb())], pressed_message_id):
        await history.track(user_id, "steps_menu")


# ================================================================ оффер


def offer_text(user: dict) -> str:
    """Экран цены: со скидкой и таймером, пока она действует, потом — обычная цена."""
    left = discount_left(user)
    if left > 0:
        price_block = fmt(
            texts.OFFER_PRICE_DISCOUNT,
            full_price=full_price_text(),
            discount_price=discount_price_text(),
            timer=format_timer(left),
            hours=format_hours(settings.discount_hours),
        )
    else:
        price_block = fmt(texts.OFFER_PRICE_FULL, full_price=full_price_text())
    return fmt(texts.OFFER, price_block=price_block)


# Экраны после шага 5 (перед ценой) по порядку: что за экран → (текст, кнопки)
SALES_PAGES = ("pitch", "product", "inside")


def _sales_page(page: str) -> ScreenPart:
    text, markup = {
        "pitch": (texts.SALES_PITCH, kb.pitch_kb),
        "product": (texts.SALES_PRODUCT, kb.product_kb),
        "inside": (texts.SALES_INSIDE, kb.inside_kb),
    }[page]
    return ScreenPart(text=text, reply_markup=markup())


async def show_sales_page(bot: Bot, user_id: int, page: str, pressed_message_id: int | None = None) -> bool:
    """Экран после шага 5: «Что дальше?» (pitch), «Покажи» (product), «Что внутри?» (inside).

    Тому, кто уже купил курс, вместо них — «Ты уже в курсе». True — экран показан.
    """
    user = await db.get_user(user_id)
    if user is None:
        return False
    if user["paid_at"]:
        part = ScreenPart(text=texts.ALREADY_BOUGHT, reply_markup=kb.back_kb(LAST_STEP))
        await show_screen(bot, user_id, [part], pressed_message_id)
        return False
    if not await show_screen(bot, user_id, [_sales_page(page)], pressed_message_id):
        return False
    await history.track(user_id, page)
    return True


async def _schedule_offer_reminder(user: dict) -> None:
    """Напоминание про курс через сутки после того, как ученик увидел экраны о нём (только один раз)."""
    ts = db.now()
    if await db.claim_offer_shown(user["user_id"], ts):
        run_at = shift_quiet(ts + settings.hours(settings.offer_reminder_hours))
        await db.add_job(user["user_id"], "offer_24", apart_from_discount_end(user, ts, run_at))


async def show_offer(
    bot: Bot,
    user_id: int,
    funnel: bool = False,
    back_step: int = 0,
    pressed_message_id: int | None = None,
    as_screen: bool = True,
) -> None:
    """Показывает экран цены («💳 Сколько стоит?»).

    funnel=True — показ в воронке после 7 дней тишины: через 24 часа после него придёт напоминание
    (после шага 5 его запускает уже экран «Что дальше?»).
    back_step — для тех, кто уже купил: шаг, куда ведёт «Назад» под «Ты уже в курсе».
    as_screen=False — отдельным сообщением, не заменяя экран (так шлёт планировщик).
    """
    user = await db.get_user(user_id)
    if user is None:
        return
    if user["paid_at"]:
        part = ScreenPart(text=texts.ALREADY_BOUGHT, reply_markup=kb.back_kb(back_step or LAST_STEP))
    else:
        part = ScreenPart(text=offer_text(user), reply_markup=kb.offer_kb(discount_active(user)))
    if as_screen:
        shown = await show_screen(bot, user_id, [part], pressed_message_id)
    else:
        shown = await send_text(bot, user_id, part.text, part.reply_markup) is not None
    if not shown or user["paid_at"]:
        return
    # as_screen=False — оффер прислал сам бот (после «давно не виделись»), а не открыл ученик
    await history.track(user_id, "offer" if as_screen else "offer_auto")
    if not user["first_offer_view_at"]:
        await db.update_user(user_id, first_offer_view_at=db.now())
    if funnel:
        await _schedule_offer_reminder(user)


# ================================================================ оплата и доступ в канал


def current_price(user: dict) -> tuple[int, int, bool]:
    """(цена в рублях, цена в звёздах, это цена со скидкой?)"""
    if discount_active(user):
        return settings.price_discount_rub, settings.price_discount_stars, True
    return settings.price_full_rub, settings.price_full_stars, False


async def create_invite_link(bot: Bot, user_id: int, old_link: str | None) -> tuple[str | None, str | None]:
    """Создаёт одноразовую ссылку в закрытый канал. Возвращает (ссылка, текст ошибки)."""
    if not settings.channel_id:
        return None, texts.ADMIN_NO_CHANNEL_ID
    try:
        # на случай, если ученика раньше удаляли из канала после возврата
        await bot.unban_chat_member(settings.channel_id, user_id, only_if_banned=True)
    except Exception:  # noqa: BLE001 — не мешает создать ссылку
        pass
    if old_link:
        try:
            await bot.revoke_chat_invite_link(settings.channel_id, old_link)
        except Exception:  # noqa: BLE001 — старая ссылка могла уже истечь
            pass
    last_error = ""
    for attempt in range(3):
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=settings.channel_id, name=f"ID {user_id}"[:32], member_limit=1
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            continue
        except (TelegramNetworkError, TelegramServerError) as exc:  # сбой связи: пробуем ещё раз
            last_error = str(exc)
            await asyncio.sleep(2 * (attempt + 1))
            continue
        except TelegramAPIError as exc:  # неверный CHANNEL_ID, у бота нет прав — повтор не поможет
            last_error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 — сбой прокси и т.п.: пробуем ещё раз
            last_error = str(exc) or exc.__class__.__name__
            await asyncio.sleep(2 * (attempt + 1))
            continue
        await db.update_user(user_id, invite_link=invite.invite_link)
        return invite.invite_link, None
    logger.error("Не удалось создать ссылку в канал для %s: %s", user_id, last_error)
    return None, last_error


async def _send_with_retry(bot: Bot, user_id: int, text: str) -> bool:
    """Отправка важного сообщения (ссылка после оплаты): при сбое связи пробуем ещё пару раз."""
    for attempt in range(3):
        try:
            return await send_text(bot, user_id, text) is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Сбой при отправке ученику %s (попытка %s): %s", user_id, attempt + 1, exc)
            await asyncio.sleep(2 * (attempt + 1))
    return False


async def grant_access(
    bot: Bot,
    user_id: int,
    *,
    amount_text: str = "",
    method_text: str = "",
    notify_payment: bool = True,
    report_undelivered: bool = True,
) -> tuple[bool, bool]:
    """Отмечает оплату, останавливает напоминания и отправляет ссылку в канал.

    Возвращает (ссылка создана?, ученик получил сообщение?).
    Админ узнаёт об оплате в любом случае — даже если со ссылкой что-то пошло не так.
    report_undelivered=False — если вызывающий сам сообщит админу, что ссылка не дошла.
    """
    user = await db.get_user(user_id)
    if user is None:
        return False, False
    already_paid = bool(user["paid_at"])
    if not already_paid:
        await db.update_user(user_id, paid_at=db.now())
        await history.track(user_id, "access")
    await db.delete_jobs(user_id)
    await db.close_pending_payments(user_id)
    card = user_card(user)

    if notify_payment:
        await notify_admin(
            bot,
            fmt(texts.ADMIN_NEW_PAYMENT, user=card, amount=amount_text, method=method_text),
            about_user=user_id,
        )

    link, error = await create_invite_link(bot, user_id, user["invite_link"])
    if link:
        text = fmt(texts.LINK_REISSUED if already_paid else texts.PAYMENT_SUCCESS, link=link)
    else:
        text = None if already_paid else texts.PAYMENT_SUCCESS_NO_LINK
        await notify_admin(
            bot,
            fmt(texts.ADMIN_INVITE_FAILED, user=card, error=esc(error), user_id=user_id),
            about_user=user_id,
        )

    delivered = False
    if text:
        delivered = await _send_with_retry(bot, user_id, text)
        if already_paid and link and delivered:
            await history.track(user_id, "access", detail="reissued")
        if link and not delivered and report_undelivered:
            await notify_admin(
                bot, fmt(texts.ADMIN_LINK_NOT_DELIVERED, user=card, user_id=user_id), about_user=user_id
            )
    return link is not None, delivered


async def revoke_access(bot: Bot, user_id: int) -> str | None:
    """Удаляет ученика из канала (после возврата). Возвращает текст ошибки или None."""
    user = await db.get_user(user_id)
    error = None
    if settings.channel_id:
        try:
            await bot.ban_chat_member(settings.channel_id, user_id)
            await bot.unban_chat_member(settings.channel_id, user_id, only_if_banned=True)
        except Exception as exc:  # noqa: BLE001 — сообщим админу
            error = str(exc) or exc.__class__.__name__
            logger.error("Не удалось удалить %s из канала: %s", user_id, error)
        if user and user["invite_link"]:
            try:
                await bot.revoke_chat_invite_link(settings.channel_id, user["invite_link"])
            except Exception:  # noqa: BLE001
                pass
    else:
        error = texts.ADMIN_NO_CHANNEL_ID
    await db.update_user(user_id, paid_at=None, invite_link=None)
    await history.track(user_id, "refund")
    return error


# ================================================================ пересылка админу


# media_group_id → (ID ученика, когда пришло). Нужно, чтобы альбом из нескольких фото
# целиком ушёл админу, а ученик получил один ответ, а не пять.
_albums: dict[str, tuple[int, float]] = {}
_admin_forward_lock = asyncio.Lock()


def claim_album(message: Message) -> bool:
    """True — сообщение продолжает уже полученный альбом: его нужно просто переслать, без ответа ученику.

    Вызывать в самом начале обработчика, до любого await: фото альбома приходят почти одновременно.
    """
    group_id = message.media_group_id
    if not group_id:
        return False
    now = time.monotonic()
    for key, (_, seen) in list(_albums.items()):
        if now - seen > 120:
            _albums.pop(key, None)
    entry = _albums.get(group_id)
    if entry is not None and entry[0] == message.from_user.id:
        return True
    _albums[group_id] = (message.from_user.id, now)
    return False


async def forward_to_admin(bot: Bot, message: Message, header_template: str | None, step: int | None = None) -> bool:
    """Пересылает сообщение ученика админу. На пересланное сообщение админ может ответить.

    Возвращает True, если сообщение дошло до админа.
    """
    if not settings.admin_id:
        logger.warning("ADMIN_ID не указан в .env — сообщение ученика не переслано")
        return False
    user_id = message.from_user.id
    # карточка ученика и его сообщение — подряд, даже если двое написали одновременно
    async with _admin_forward_lock:
        if header_template:
            user = await db.get_user(user_id)
            await notify_admin(
                bot, fmt(header_template, user=user_card(user, message.from_user), step=step), about_user=user_id
            )
        forwarded = await admin_call(lambda: message.forward(settings.admin_id))
    if forwarded is None:
        return False
    await db.link_admin_message(forwarded.message_id, user_id)
    return True
