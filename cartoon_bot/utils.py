"""Вспомогательные функции: время, ночные часы, форматирование цен и текстов."""
import html
import logging
import os
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import texts
from config import BASE_DIR, settings

logger = logging.getLogger(__name__)

TZ = ZoneInfo(settings.timezone)


class _KeepMissing(dict):
    """Если в тексте есть {слово}, которое бот не знает, оставляем его как есть, а не падаем."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fmt(template: str, **values) -> str:
    """Подставляет значения в текст из texts.py и не ломается от опечаток в фигурных скобках."""
    try:
        return template.format_map(_KeepMissing(**values))
    except (ValueError, IndexError, AttributeError) as exc:
        logger.warning("Не получилось подставить данные в текст (%s): %.60s...", exc, template)
        return template


def esc(value: object) -> str:
    """Экранирует данные пользователя (имя и т.п.) для HTML-разметки."""
    return html.escape(str(value), quote=False)


# Подпись к фото/видео в Telegram — не длиннее 1024 видимых символов
CAPTION_LIMIT = 1024


def visible_length(text: str) -> int:
    """Длина текста так, как её считает Telegram: без тегов разметки, с раскрытыми &lt; &amp;,
    в единицах UTF-16 (многие эмодзи считаются за 2) — так подсчёт не занижает длину."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2


# ---------------------------------------------------------------- ночные часы


def is_quiet(ts: int) -> bool:
    """True, если момент ts попадает в «ночь» (по умолчанию 23:00–09:00 по Москве)."""
    if not settings.quiet_enabled:
        return False
    hour = datetime.fromtimestamp(ts, TZ).hour
    start, end = settings.quiet_start, settings.quiet_end
    if start > end:  # ночь переходит через полночь, например 23 → 9
        return hour >= start or hour < end
    return start <= hour < end


def shift_quiet(ts: int) -> int:
    """Если ts ночью — переносит на ближайшее утро в QUIET_SEND_AT (по умолчанию 10:00)."""
    if not is_quiet(ts):
        return ts
    local = datetime.fromtimestamp(ts, TZ)
    target = local.replace(hour=settings.quiet_send_at, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return int(target.timestamp())


def shift_quiet_back(ts: int) -> int:
    """Если ts ночью — переносит на вечер ПЕРЕД этой ночью (за 30 минут до QUIET_START, по умолчанию 22:30)."""
    if not is_quiet(ts):
        return ts
    local = datetime.fromtimestamp(ts, TZ)
    evening = local.replace(hour=settings.quiet_start, minute=0, second=0, microsecond=0) - timedelta(minutes=30)
    if local.hour < settings.quiet_start:
        evening -= timedelta(days=1)
    return int(evening.timestamp())


# ---------------------------------------------------------------- форматирование


def format_timer(seconds: int) -> str:
    """Оставшееся время: «1 дн 3 ч», «23 ч 15 мин», «40 мин»."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return texts.TIMER_LESS_THAN_MINUTE
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} {texts.TIMER_DAYS}")
    if hours:
        parts.append(f"{hours} {texts.TIMER_HOURS}")
    if minutes and not days:
        parts.append(f"{minutes} {texts.TIMER_MINUTES}")
    return " ".join(parts)


def format_number(value: int) -> str:
    """10000 → «10 000» (с неразрывным пробелом)."""
    return f"{int(value):,}".replace(",", " ")


def format_price(rub: int, stars: int) -> str:
    template = texts.PRICE_FORMAT_STARS if settings.payment_mode == "stars" else texts.PRICE_FORMAT_LINK
    return fmt(template, rub=format_number(rub), stars=format_number(stars))


def full_price_text() -> str:
    return format_price(settings.price_full_rub, settings.price_full_stars)


def discount_price_text() -> str:
    return format_price(settings.price_discount_rub, settings.price_discount_stars)


# ---------------------------------------------------------------- проверка texts.py при запуске

_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "tg-spoiler", "span", "blockquote", "tg-emoji",
}
_BAD_LT = re.compile(r"<(?![a-zA-Z/])")
_BAD_AMP = re.compile(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)")


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in _ALLOWED_TAGS:
            self.problems.append(f"тег <{tag}> Telegram не поддерживает")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"лишний или не на месте закрывающий тег </{tag}>")
            return
        self.stack.pop()


def _check_one(text: str) -> list[str]:
    problems = []
    if _BAD_LT.search(text):
        problems.append("символ < (замени на &lt;)")
    if _BAD_AMP.search(text):
        problems.append("символ & (замени на &amp;)")
    checker = _TagChecker()
    try:
        checker.feed(text)
        checker.close()
    except Exception as exc:  # noqa: BLE001 — любая ошибка разбора = проблема в тексте
        problems.append(f"не получилось разобрать разметку ({exc})")
    problems.extend(checker.problems)
    if checker.stack:
        problems.append("не закрыт тег <" + ">, <".join(checker.stack) + ">")
    length = visible_length(text)
    if length > 4096:
        problems.append(f"слишком длинный текст: {length} символов (можно 4096)")
    return problems


# Эти тексты Telegram показывает как обычный текст, без разметки: кнопки, команды, счёт, всплывающие окна
_PLAIN_PREFIXES = ("BTN_", "CMD_", "PRECHECKOUT_", "INVOICE_")
# Незаполненное место из шаблона: [Название курса], [ответ], [@username]…
_PLACEHOLDER = re.compile(r"\[[^\[\]\n]*[А-Яа-яЁёA-Za-z@][^\[\]\n]*\]")


def _placeholders(value: object) -> list[str]:
    """Все [места для заполнения] в тексте (или в списке текстов)."""
    if isinstance(value, str):
        return _PLACEHOLDER.findall(value)
    if isinstance(value, (list, tuple)):
        return [found for item in value for found in _placeholders(item)]
    return []


_ALERT_NAMES = {"BUTTON_EXPIRED", "ADMIN_ALREADY_PROCESSED", "DOWNLOAD_FAILED", "ADMIN_BROADCAST_NOT_RUNNING"}


def check_texts() -> list[str]:
    """Проверяет все тексты из texts.py: разметку и длину. Возвращает список проблем."""
    problems = []
    for name in dir(texts):
        if not name.isupper():
            continue
        value = getattr(texts, name)
        for key, text in value.items() if isinstance(value, dict) else [(None, value)]:
            found = list(dict.fromkeys(_placeholders(text)))
            if found:
                label = name if key is None else f"{name}[{key}]"
                more = f" и ещё {len(found) - 3}" if len(found) > 3 else ""
                problems.append(f"{label}: не заполнено {', '.join(found[:3])}{more} — впиши свой текст вместо скобок")
        if name.startswith(_PLAIN_PREFIXES) or name in _ALERT_NAMES:
            if name in _ALERT_NAMES and isinstance(value, str) and len(value) > 200:
                problems.append(f"{name}: длиннее 200 символов ({len(value)}) — Telegram не покажет окно")
            continue
        items = value.items() if isinstance(value, dict) else [(None, value)]
        for key, text in items:
            label = name if key is None else f"{name}[{key}]"
            # шаг из нескольких сообщений — список текстов: проверяем каждое сообщение
            parts = list(enumerate(text, 1)) if isinstance(text, (list, tuple)) else [(None, text)]
            for number, part in parts:
                if not isinstance(part, str):
                    continue
                part_label = label if number is None else f"{label}, сообщение {number}"
                problems.extend(f"{part_label}: {problem}" for problem in _check_one(part))
    if len(texts.INVOICE_TITLE) > 32:
        problems.append(f"INVOICE_TITLE: длиннее 32 символов ({len(texts.INVOICE_TITLE)}) — Telegram обрежет название")
    problems.extend(_check_media_files())
    problems.extend(_check_video_captions())
    return problems


def _check_video_captions() -> list[str]:
    """Шаг с одним видео/GIF приходит одним сообщением, только если текст влезает в подпись (1024 символа)."""
    problems = []
    step_media = getattr(texts, "STEP_MEDIA", None) or {}
    hints = getattr(texts, "STEP_DISCOUNT_HINTS", None) or {}
    if not isinstance(step_media, dict):
        return problems
    for step, items in step_media.items():
        items = [items] if isinstance(items, dict) else items
        text = texts.STEP_TEXTS.get(step)
        if not isinstance(items, (list, tuple)) or len(items) != 1 or not isinstance(text, str):
            continue
        if not isinstance(items[0], dict) or items[0].get("type") not in ("video", "animation"):
            continue
        hint = hints.get(step)
        length = visible_length(f"{text} {hint}" if hint else text)
        if length > CAPTION_LIMIT:
            problems.append(
                f"STEP_TEXTS[{step}]: {length} символов (вместе с фразой из STEP_DISCOUNT_HINTS) — в подпись "
                f"к видео влезает {CAPTION_LIMIT}, поэтому видео и текст придут разными сообщениями"
            )
    return problems


MEDIA_KINDS = ("photo", "video", "animation", "document")
# Сколько Telegram разрешает загрузить боту: фото — до 10 МБ, остальные файлы — до 50 МБ
_PHOTO_LIMIT_MB = 10
_FILE_LIMIT_MB = 50


def _check_media_files() -> list[str]:
    """Картинки/видео из texts.py: правильная запись, файл лежит в папке бота, размер в лимитах Telegram."""
    problems = []
    groups = [("GREETING_MEDIA", getattr(texts, "GREETING_MEDIA", None))]
    step_media = getattr(texts, "STEP_MEDIA", None) or {}
    if isinstance(step_media, dict):
        groups += [(f"STEP_MEDIA[{step}]", items) for step, items in step_media.items()]
    else:
        problems.append("STEP_MEDIA: нужен вид {1: [...], 2: [...], ...} — не удаляй фигурные и квадратные скобки")
    downloads = getattr(texts, "STEP_DOWNLOADS", None) or {}
    if isinstance(downloads, dict):
        for step, download in downloads.items():
            if not isinstance(download, dict) or not download.get("file") or not download.get("button"):
                problems.append(f'STEP_DOWNLOADS[{step}]: нужны «file» (имя файла) и «button» (надпись на кнопке)')
                continue
            caption = download.get("caption")
            if caption is not None and not isinstance(caption, str):
                problems.append(f"STEP_DOWNLOADS[{step}]: подпись (caption) — это текст в кавычках")
            elif caption:
                problems.extend(f"STEP_DOWNLOADS[{step}], подпись: {problem}" for problem in _check_one(caption))
                if visible_length(caption) > CAPTION_LIMIT:
                    problems.append(
                        f"STEP_DOWNLOADS[{step}], подпись: {visible_length(caption)} символов (можно {CAPTION_LIMIT})"
                    )
            groups.append((f"STEP_DOWNLOADS[{step}]", [{"type": "document", "file": download["file"]}]))
    else:
        problems.append("STEP_DOWNLOADS: нужен вид {1: {...}, ...}")
    for label, items in groups:
        if items is None:
            continue
        if isinstance(items, dict):  # одна запись без квадратных скобок — тоже подойдёт
            items = [items]
        if not isinstance(items, (list, tuple)):
            problems.append(f"{label}: нужен список в квадратных скобках [ {{...}} ] (или пустые [])")
            continue
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("type") not in MEDIA_KINDS
                or not (item.get("file") or item.get("file_id"))
            ):
                problems.append(f'{label}: неправильная запись {item!r} — нужен вид {{"type": "photo", "file": "имя файла"}}')
                continue
            if item.get("file_id"):
                continue
            path = Path(item["file"])
            if not path.is_absolute():
                path = BASE_DIR / path
            # точное совпадение имени: на сервере (Linux) «шаг 0.png» и «Шаг 0.png» — разные файлы
            if not path.is_file() or path.name not in os.listdir(path.parent):
                problems.append(f"{label}: файл «{item['file']}» не найден в папке бота (проверь и большие/маленькие буквы)")
                continue
            size_mb = path.stat().st_size / 1024 / 1024
            limit_mb = _PHOTO_LIMIT_MB if item["type"] == "photo" else _FILE_LIMIT_MB
            if size_mb > limit_mb:
                problems.append(
                    f"{label}: файл «{item['file']}» весит {size_mb:.1f} МБ — Telegram принимает до {limit_mb} МБ"
                )
    return problems
