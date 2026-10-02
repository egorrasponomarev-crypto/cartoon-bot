"""Настройки бота.

Все значения берутся из файла .env (образец — .env.example).
Этот файл менять не нужно: меняй значения в .env.
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _str(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"Ошибка в .env: {name} должно быть целым числом, а сейчас там: {value!r}")


def _float(name: str, default: float) -> float:
    value = _str(name).replace(",", ".")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise SystemExit(f"Ошибка в .env: {name} должно быть числом, а сейчас там: {value!r}")


def _bool(name: str, default: bool) -> bool:
    value = _str(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "да")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    channel_id: int

    payment_mode: str  # "preorder" (предзапись через чат с автором), "stars" или "link"
    price_full_rub: int
    price_discount_rub: int
    price_full_stars: int
    price_discount_stars: int
    payment_link_full: str
    payment_link_discount: str

    discount_hours: float
    step_reminder_1_hours: float
    step_reminder_2_hours: float
    inactivity_hours: float
    offer_reminder_hours: float
    last_call_hours: float
    sales_reminders_for_all: bool

    quiet_hours: bool
    quiet_start: int
    quiet_end: int
    quiet_send_at: int
    timezone: str

    test_mode: bool
    test_hour_seconds: int

    broadcast_delay: float
    db_path: str
    proxy: str
    help_url: str  # куда ведёт кнопка «Нужна помощь»; пусто — в чат с админом (по его username)
    # Картинки «над текстом» (когда текст шага длиннее подписи к фото): бот кладёт их в папку,
    # которую отдаёт веб-сервер, и показывает по ссылке. Пусто — картинка придёт отдельным сообщением.
    media_web_dir: str
    media_base_url: str

    @property
    def hour(self) -> int:
        """Сколько секунд длится один «час» (в тестовом режиме — меньше)."""
        return self.test_hour_seconds if self.test_mode else 3600

    def hours(self, amount: float) -> int:
        """Переводит «часы» из настроек в секунды с учётом тестового режима."""
        return int(amount * self.hour)

    @property
    def quiet_enabled(self) -> bool:
        """Ночное правило в тестовом режиме не действует, иначе тест вечером «застрянет» до утра."""
        return self.quiet_hours and not self.test_mode

    @property
    def scheduler_tick(self) -> int:
        """Как часто (в секундах) бот проверяет, не пора ли отправить напоминания."""
        return 5 if self.test_mode else 30


def _load() -> Settings:
    token = _str("BOT_TOKEN")
    if not token:
        raise SystemExit("Не указан BOT_TOKEN в файле .env. Скопируй .env.example в .env и вставь токен от BotFather.")
    from aiogram.utils.token import TokenValidationError, validate_token

    try:
        validate_token(token)
    except TokenValidationError:
        raise SystemExit("BOT_TOKEN в .env выглядит неправильно — скопируй его из @BotFather целиком, без пробелов и кавычек.")

    payment_mode = _str("PAYMENT_MODE", "preorder").lower()
    if payment_mode not in ("preorder", "stars", "link"):
        raise SystemExit(
            f'Ошибка в .env: PAYMENT_MODE должно быть "preorder", "stars" или "link", а сейчас: {payment_mode!r}'
        )

    admin_id = _int("ADMIN_ID", 0)
    if payment_mode == "link" and not admin_id:
        raise SystemExit("Для PAYMENT_MODE=link нужен ADMIN_ID: кто-то должен подтверждать оплаты.")

    quiet_start, quiet_end, quiet_send_at = _int("QUIET_START", 23), _int("QUIET_END", 9), _int("QUIET_SEND_AT", 10)
    if not all(0 <= h <= 23 for h in (quiet_start, quiet_end, quiet_send_at)):
        raise SystemExit("Ошибка в .env: QUIET_START, QUIET_END и QUIET_SEND_AT — это часы от 0 до 23.")
    if quiet_start > quiet_end:
        send_inside_night = quiet_send_at >= quiet_start or quiet_send_at < quiet_end
    else:
        send_inside_night = quiet_start <= quiet_send_at < quiet_end
    if send_inside_night:
        raise SystemExit("Ошибка в .env: QUIET_SEND_AT должно быть вне ночных часов (не раньше QUIET_END и не позже QUIET_START).")

    timezone = _str("TIMEZONE") or "Europe/Moscow"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception:  # ZoneInfoNotFoundError, ValueError
        raise SystemExit(f"Ошибка в .env: TIMEZONE={timezone!r} — неизвестный часовой пояс. Пример: Europe/Moscow")

    help_url = _str("HELP_URL")
    if help_url.startswith("@"):
        help_url = "https://t.me/" + help_url[1:]
    elif help_url.startswith(("t.me/", "telegram.me/")):
        help_url = "https://" + help_url
    if help_url and not help_url.startswith(("https://", "http://", "tg://")):
        raise SystemExit("Ошибка в .env: HELP_URL должен быть ссылкой, например https://t.me/username или @username.")

    media_web_dir, media_base_url = _str("MEDIA_WEB_DIR"), _str("MEDIA_BASE_URL").rstrip("/")
    if bool(media_web_dir) != bool(media_base_url):
        raise SystemExit("Ошибка в .env: MEDIA_WEB_DIR и MEDIA_BASE_URL заполняются вместе (или обе пустые).")
    if media_base_url and not media_base_url.startswith(("http://", "https://")):
        raise SystemExit("Ошибка в .env: MEDIA_BASE_URL должен начинаться с http:// или https://")

    proxy = _str("PROXY")
    if proxy:
        from python_socks import parse_proxy_url

        try:
            parse_proxy_url(proxy)
        except ValueError:
            raise SystemExit(
                "Ошибка в .env: PROXY должен быть вида socks5://логин:пароль@IP:порт или http://IP:порт (порт обязателен). "
                "MTProto-прокси (tg://proxy…) для ботов не подходят. Спецсимволы в пароле закодируй: @ → %40, # → %23, / → %2F."
            )

    db_path = Path(_str("DB_PATH", "bot.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    settings = Settings(
        bot_token=token,
        admin_id=admin_id,
        channel_id=_int("CHANNEL_ID", 0),
        payment_mode=payment_mode,
        price_full_rub=_int("PRICE_FULL_RUB", 10000),
        price_discount_rub=_int("PRICE_DISCOUNT_RUB", 4990),
        price_full_stars=_int("PRICE_FULL_STARS", 5000),
        price_discount_stars=_int("PRICE_DISCOUNT_STARS", 2500),
        payment_link_full=_str("PAYMENT_LINK_FULL"),
        payment_link_discount=_str("PAYMENT_LINK_DISCOUNT"),
        discount_hours=_float("DISCOUNT_HOURS", 24),
        step_reminder_1_hours=_float("STEP_REMINDER_1_HOURS", 24),
        step_reminder_2_hours=_float("STEP_REMINDER_2_HOURS", 72),
        inactivity_hours=_float("INACTIVITY_HOURS", 168),
        offer_reminder_hours=_float("OFFER_REMINDER_HOURS", 24),
        last_call_hours=_float("LAST_CALL_HOURS", 3),
        sales_reminders_for_all=_bool("SALES_REMINDERS_FOR_ALL", True),
        quiet_hours=_bool("QUIET_HOURS", True),
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        quiet_send_at=quiet_send_at,
        timezone=timezone,
        test_mode=_bool("TEST_MODE", False),
        test_hour_seconds=max(1, _int("TEST_HOUR_SECONDS", 60)),
        broadcast_delay=max(0.04, _float("BROADCAST_DELAY", 0.05)),
        db_path=str(db_path),
        proxy=proxy,
        help_url=help_url,
        media_web_dir=media_web_dir,
        media_base_url=media_base_url,
    )
    return settings


settings = _load()
