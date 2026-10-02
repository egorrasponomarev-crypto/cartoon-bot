"""История действий учеников: что и когда открыли, нажали, получили.

Нужна админу: /user — история одного ученика, /export — таблицы по всем ученикам для Excel.
Запись истории никогда не мешает самому боту: если строчка не записалась, бот работает дальше.
"""
import asyncio
import csv
import io
import logging
from datetime import datetime

import db
import texts
from config import settings
from utils import TZ, esc, fmt, format_timer, plural, visible_length

logger = logging.getLogger(__name__)

STEPS = range(1, 6)
# Сколько последних действий показывать в /user (вся история — в /export)
REPORT_EVENTS = 40
# Запас до лимита Telegram в 4096 символов
REPORT_LIMIT = 4000
# В таблицу «история» — не больше стольких последних действий (это около 7 МБ):
# иначе на большой базе файл не пройдёт в Telegram (лимит 50 МБ), а сборка займёт много памяти
EXPORT_EVENTS_LIMIT = 100_000
# Отметка у записи «Вступить»: уведомление админу дошло (о следующих нажатиях этого ученика не пишем)
PREORDER_NOTIFIED = "admin"


async def track(user_id: int, kind: str, step: int | None = None, detail: str | None = None) -> None:
    """Записывает действие ученика в историю. Сбой записи ничего не ломает."""
    try:
        await db.add_event(user_id, kind, step, detail)
    except Exception:  # noqa: BLE001 — история не важнее самого бота
        logger.exception("Не удалось записать в историю %s (ученик %s)", kind, user_id)


def _time(ts: int | None, with_year: bool = False) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M")


def event_label(event: dict) -> str:
    """Действие из истории понятными словами (обычный текст, без разметки)."""
    kind, step, detail = event["kind"], event.get("step"), event.get("detail") or ""
    if kind == "start" and detail:
        return fmt(texts.HISTORY_START_WITH_LABEL, label=detail)
    if kind == "reminder":
        detail = fmt(texts.HISTORY_REMINDERS.get(detail, detail), step=step if step is not None else "")
    if kind == "offer" and step:
        return fmt(texts.HISTORY_OFFER_FROM_STEP, step=step)
    if kind == "ask" and step != 1:
        return texts.HISTORY_ASK_AUTHOR  # «Написать автору» / «Нужна помощь», а не «Есть вопрос» на экране цены
    return fmt(texts.HISTORY_EVENTS.get(kind, kind), step=step if step is not None else "", detail=detail)


def _first(summary: dict, kind: str) -> tuple[int | None, int]:
    """(когда впервые, сколько раз) — по всем записям этого вида, с любым номером шага."""
    rows = [row for (row_kind, _), row in summary.items() if row_kind == kind]
    if not rows:
        return None, 0
    return min(row["first_ts"] for row in rows), sum(row["count"] for row in rows)


def _legacy(summary: dict) -> bool:
    """Ученик пришёл до того, как бот начал вести историю: нет записи о первом входе."""
    return ("joined", None) not in summary


def _step_cell(user: dict, summary: dict, step: int) -> tuple[int | None, int, bool]:
    """(когда шаг открыт впервые, сколько раз, открыт до начала подробной истории)."""
    row = summary.get(("step", step))
    if row:
        return row["first_ts"], row["count"], False
    # «самый дальний шаг» из старых записей — только для тех, кто пришёл до начала истории
    return None, 0, _legacy(summary) and step <= (user.get("max_step_opened") or 0)


def _offer(user: dict, summary: dict) -> tuple[int | None, int]:
    """(когда ученик впервые сам открыл оффер, сколько раз). Оффер, присланный ботом, сюда не входит."""
    ts, count = _first(summary, "offer")
    old = user.get("first_offer_view_at") if _legacy(summary) else None
    if old and (not ts or old < ts):
        ts, count = old, max(count, 1)
    return ts, count


def _times(count: int) -> str:
    return f"{count} {plural(count, texts.TIMES_WORDS)}"


async def user_report(user: dict) -> str:
    """Карточка ученика для админа: шаги, оффер, скачивания и последние действия."""
    user_id = user["user_id"]
    summary = (await db.event_summary(user_id)).get(user_id, {})
    now = db.now()

    step_lines = []
    for step in STEPS:
        first_ts, count, early = _step_cell(user, summary, step)
        if first_ts:
            line = fmt(texts.HISTORY_STEP_OPENED, step=step, time=_time(first_ts))
            if count > 1:
                line += fmt(texts.HISTORY_STEP_TIMES, times=_times(count))
            step_lines.append(line)
        elif early:
            step_lines.append(fmt(texts.HISTORY_STEP_EARLY, step=step))
        else:
            step_lines.append(fmt(texts.HISTORY_STEP_NOT_OPENED, step=step))

    facts = []
    for step, download in sorted((getattr(texts, "STEP_DOWNLOADS", None) or {}).items()):
        row = summary.get(("file", step))
        name = esc(download.get("filename") or download.get("file") or "") if isinstance(download, dict) else ""
        if row:
            facts.append(fmt(texts.HISTORY_FACT_FILE, name=name, time=_time(row["first_ts"])))
        else:
            facts.append(fmt(texts.HISTORY_FACT_NO_FILE, name=name))
    offer_ts, offer_count = _offer(user, summary)
    pages = []
    for page, name in texts.HISTORY_SALES_PAGES.items():
        seen = offer_ts if page == "offer" else _first(summary, page)[0]
        pages.append(f"{name} {'✅' if seen else '▫️'}")
    facts.append(fmt(texts.HISTORY_FACT_SALES, pages=" → ".join(pages)))
    if offer_ts:
        facts.append(fmt(texts.HISTORY_FACT_OFFER, time=_time(offer_ts), times=_times(offer_count)))
    else:
        facts.append(texts.HISTORY_FACT_NO_OFFER)
    auto_ts, _ = _first(summary, "offer_auto")
    if auto_ts:
        facts.append(fmt(texts.HISTORY_FACT_OFFER_AUTO, time=_time(auto_ts)))
    if user.get("finished_at"):
        facts.append(fmt(texts.HISTORY_FACT_FINISH, time=_time(user["finished_at"])))
    preorder_ts, preorder_count = _first(summary, "preorder")
    if preorder_ts:
        facts.append(fmt(texts.HISTORY_FACT_PREORDER, time=_time(preorder_ts), times=_times(preorder_count)))
    _, questions = _first(summary, "question")
    if questions:
        facts.append(fmt(texts.HISTORY_FACT_QUESTIONS, count=questions))
    until = user.get("discount_until") or 0
    if until > now:
        facts.append(fmt(texts.HISTORY_FACT_DISCOUNT, time=_time(until), left=format_timer(until - now)))
    elif until:
        facts.append(fmt(texts.HISTORY_FACT_DISCOUNT_ENDED, time=_time(until)))
    if user.get("paid_at"):
        facts.append(fmt(texts.HISTORY_FACT_ACCESS, time=_time(user["paid_at"])))
    if user.get("blocked"):
        facts.append(texts.HISTORY_FACT_BLOCKED)

    total = await db.count_events(user_id)
    events = await db.user_events(user_id, limit=REPORT_EVENTS)
    event_lines = [f"{_time(event['ts'])} — {esc(event_label(event))}" for event in events]

    def build(lines: list[str]) -> str:
        shown = len(lines)
        if not lines:
            events_text = texts.HISTORY_NO_EVENTS
        elif shown < total:
            events_text = fmt(texts.HISTORY_EVENTS_CUT, shown=shown, total=total) + "\n" + "\n".join(lines)
        else:
            events_text = "\n".join(lines)
        return fmt(
            texts.HISTORY_REPORT,
            name=esc(user.get("first_name") or "—"),
            username=f"@{esc(user['username'])}" if user.get("username") else texts.ADMIN_NO_USERNAME,
            user_id=user_id,
            source=esc(user.get("source") or texts.ADMIN_NO_SOURCE),
            created=_time(user.get("created_at")),
            last=_time(user.get("last_activity_at")),
            steps="\n".join(step_lines),
            facts="\n".join(facts),
            events=events_text,
        )

    text = build(event_lines)
    # длинная история не должна упереться в лимит сообщения Telegram — убираем самые старые строчки
    while visible_length(text) > REPORT_LIMIT and event_lines:
        event_lines = event_lines[1:]
        text = build(event_lines)
    return text


# Клетку, которая начинается с этих знаков, Excel может принять за формулу (например, имя ученика «=…»).
# Такие клетки бот записывает как текст — с апострофом в начале
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _csv(rows: list[list]) -> bytes:
    """Таблица для Excel: разделитель «;» и метка UTF-8 — так русские буквы открываются без настройки."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerows([[_cell(value) for value in row] for row in rows])
    return out.getvalue().encode("utf-8-sig")


def _build_export(users: list[dict], summaries: dict, events: list[dict], now: int) -> list[tuple[str, bytes]]:
    """Собирает обе таблицы. Работает в отдельном потоке: на большой базе это занимает секунды."""
    rows = [list(texts.EXPORT_USERS_HEADER)]
    for user in users:
        summary = summaries.get(user["user_id"], {})
        steps = []
        for step in STEPS:
            first_ts, _, early = _step_cell(user, summary, step)
            steps.append(_time(first_ts, True) if first_ts else (texts.EXPORT_STEP_EARLY if early else ""))
        file_ts, _ = _first(summary, "file")
        offer_ts, offer_count = _offer(user, summary)
        preorder_ts, _ = _first(summary, "preorder")
        ask_row = summary.get(("ask", 1))  # только «❓ Есть вопрос» на экране цены
        ask_ts = ask_row["first_ts"] if ask_row else None
        _, questions = _first(summary, "question")
        pages = [_time(_first(summary, page)[0], True) for page in ("pitch", "product", "inside")]
        rows.append(
            [
                user["user_id"],
                user.get("first_name") or "",
                user.get("username") or "",
                user.get("source") or "",
                _time(user.get("created_at"), True),
                _time(user.get("last_activity_at"), True),
                *steps,
                _time(file_ts, True),
                _time(user.get("finished_at"), True),
                *pages,
                _time(offer_ts, True),
                offer_count,
                _time(preorder_ts, True),
                _time(ask_ts, True),
                questions,
                _time(user.get("discount_until"), True),
                _time(user.get("paid_at"), True),
                texts.YES if user.get("blocked") else "",
            ]
        )
    names = {user["user_id"]: user for user in users}
    history = [list(texts.EXPORT_HISTORY_HEADER)]
    for event in events:
        user = names.get(event["user_id"], {})
        history.append(
            [
                _time(event["ts"], True),
                event["user_id"],
                user.get("first_name") or "",
                user.get("username") or "",
                event_label(event),
            ]
        )
    date = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d")
    return [
        (f"{texts.EXPORT_USERS_FILE}-{date}.csv", _csv(rows)),
        (f"{texts.EXPORT_HISTORY_FILE}-{date}.csv", _csv(history)),
    ]


async def export_files() -> tuple[list[tuple[str, bytes]], bool]:
    """Две таблицы: ученики (строка на ученика, когда открыт каждый шаг) и их действия по порядку.

    Возвращает (файлы, история урезана до EXPORT_EVENTS_LIMIT последних действий). Сам админ в таблицы не попадает.
    Учеников нет — файлов нет.
    """
    admin_id = settings.admin_id or None
    users = [user for user in await db.all_users() if user["user_id"] != admin_id]
    if not users:
        return [], False
    summaries = await db.event_summary()
    events = await db.recent_events(EXPORT_EVENTS_LIMIT, exclude_user=admin_id)
    capped = len(events) >= EXPORT_EVENTS_LIMIT and await db.count_all_events(exclude_user=admin_id) > len(events)
    # таблицы собираем в отдельном потоке — бот в это время продолжает отвечать ученикам
    files = await asyncio.to_thread(_build_export, users, summaries, events, db.now())
    return files, capped
