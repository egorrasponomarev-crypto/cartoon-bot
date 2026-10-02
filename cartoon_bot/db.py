"""Работа с базой данных SQLite.

Здесь хранятся ученики, их прогресс, запланированные напоминания и оплаты.
Файл базы (по умолчанию bot.db) создаётся автоматически при первом запуске.
"""
import json
import time
from typing import Any, Iterable

import aiosqlite

_db: aiosqlite.Connection | None = None

# Какие колонки можно менять через update_user (защита от опечаток в коде).
USER_FIELDS = {
    "first_name",
    "username",
    "source",
    "last_activity_at",
    "current_step",
    "step_opened_at",
    "max_step_opened",
    "finished_at",
    "discount_until",
    "offer_shown_at",
    "first_offer_view_at",
    "inactive_sent",
    "paid_at",
    "blocked",
    "invite_link",
    "menu_msg_id",
    "old_menu",
}

# Колонки, добавленные после первого запуска: в старой базе их нужно дописать
_ADDED_USER_COLUMNS = {
    "screen": "TEXT",  # сообщения текущего «экрана» (шага, оффера) — их бот заменяет при переходе
    "menu_msg_id": "INTEGER",  # сообщение с нижним меню (чтобы не копились одинаковые)
    "old_menu": "INTEGER NOT NULL DEFAULT 0",  # 1 — у ученика может остаться нижнее меню прошлой версии
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id             INTEGER PRIMARY KEY,
    first_name          TEXT,
    username            TEXT,
    source              TEXT,
    created_at          INTEGER NOT NULL,
    last_activity_at    INTEGER NOT NULL,
    current_step        INTEGER NOT NULL DEFAULT 1,
    step_opened_at      INTEGER,
    max_step_opened     INTEGER NOT NULL DEFAULT 0,
    finished_at         INTEGER,
    discount_until      INTEGER,
    offer_shown_at      INTEGER,
    first_offer_view_at INTEGER,
    inactive_sent       INTEGER NOT NULL DEFAULT 0,
    paid_at             INTEGER,
    blocked             INTEGER NOT NULL DEFAULT 0,
    invite_link         TEXT,
    screen              TEXT,
    menu_msg_id         INTEGER,
    old_menu            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    run_at     INTEGER NOT NULL,
    step       INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_run_at ON jobs (run_at);
-- у одного ученика не может быть двух одинаковых напоминаний (защита от двойных нажатий)
DELETE FROM jobs WHERE id NOT IN (SELECT MIN(id) FROM jobs GROUP BY user_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_user_kind ON jobs (user_id, kind);

CREATE TABLE IF NOT EXISTS payments (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                    INTEGER NOT NULL,
    mode                       TEXT NOT NULL,
    amount                     INTEGER NOT NULL,
    currency                   TEXT NOT NULL,
    is_discount                INTEGER NOT NULL DEFAULT 0,
    telegram_payment_charge_id TEXT UNIQUE,
    status                     TEXT NOT NULL,
    created_at                 INTEGER NOT NULL,
    updated_at                 INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments (user_id);

CREATE TABLE IF NOT EXISTS admin_links (
    admin_message_id INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    created_at       INTEGER NOT NULL
);
"""


def now() -> int:
    return int(time.time())


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("База данных ещё не открыта: сначала вызови init_db()")
    return _db


async def init_db(path: str) -> None:
    global _db
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=5000")
    await _db.executescript(SCHEMA)
    async with _db.execute("PRAGMA table_info(users)") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for column, declaration in _ADDED_USER_COLUMNS.items():
        if column not in existing:
            await _db.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
            if column == "old_menu":
                # все, кто пришёл до этого обновления, видели нижнее меню — бот уберёт его при их следующем действии
                await _db.execute("UPDATE users SET old_menu = 1")
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def _fetchone(sql: str, params: Iterable[Any] = ()) -> dict | None:
    async with _conn().execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _fetchall(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    async with _conn().execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Выполняет запрос и возвращает число изменённых строк."""
    cur = await _conn().execute(sql, tuple(params))
    await _conn().commit()
    count = cur.rowcount
    await cur.close()
    return count


# ---------------------------------------------------------------- ученики


async def get_user(user_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def create_user(user_id: int, first_name: str, username: str | None, discount_until: int) -> bool:
    """Создаёт ученика. Возвращает True, если он действительно новый."""
    ts = now()
    inserted = await _execute(
        """INSERT OR IGNORE INTO users
           (user_id, first_name, username, created_at, last_activity_at, discount_until)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, first_name, username, ts, ts, discount_until),
    )
    return inserted == 1


async def touch_user(user_id: int, first_name: str, username: str | None) -> None:
    """Отмечает активность ученика (любое сообщение или нажатие кнопки)."""
    await _execute(
        "UPDATE users SET first_name = ?, username = ?, last_activity_at = ?, blocked = 0 WHERE user_id = ?",
        (first_name, username, now(), user_id),
    )


async def set_source_if_empty(user_id: int, source: str) -> None:
    await _execute(
        "UPDATE users SET source = ? WHERE user_id = ? AND (source IS NULL OR source = '')",
        (source, user_id),
    )


async def update_user(user_id: int, **fields: Any) -> None:
    unknown = set(fields) - USER_FIELDS
    if unknown:
        raise ValueError(f"Неизвестные поля пользователя: {unknown}")
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    await _execute(f"UPDATE users SET {assignments} WHERE user_id = ?", (*fields.values(), user_id))


async def get_screen_items(user_id: int) -> list[tuple[int, str, int | None]]:
    """Сообщения текущего экрана ученика: [(message_id, тип, когда отправлено), ...]."""
    row = await _fetchone("SELECT screen FROM users WHERE user_id = ?", (user_id,))
    if not row or not row["screen"]:
        return []
    try:
        items = []
        for entry in json.loads(row["screen"]):
            sent_at = entry[2] if len(entry) > 2 and entry[2] else None
            items.append((int(entry[0]), str(entry[1]), int(sent_at) if sent_at else None))
        return items
    except (ValueError, TypeError, IndexError, KeyError):
        return []


async def get_screen(user_id: int) -> list[tuple[int, str]]:
    """Сообщения текущего экрана ученика: [(message_id, тип), ...]."""
    return [(message_id, kind) for message_id, kind, _ in await get_screen_items(user_id)]


async def _save_screen(user_id: int, items: list[tuple[int, str, int | None]]) -> None:
    data = [[message_id, kind, sent_at] for message_id, kind, sent_at in items]
    await _execute("UPDATE users SET screen = ? WHERE user_id = ?", (json.dumps(data), user_id))


async def set_screen(user_id: int, items: list[tuple[int, str]]) -> None:
    ts = now()
    await _save_screen(user_id, [(message_id, kind, ts) for message_id, kind in items])


async def append_screen(user_id: int, message_id: int, kind: str) -> None:
    """Добавляет сообщение к текущему экрану (например, счёт на оплату под оффером)."""
    await _save_screen(user_id, [*await get_screen_items(user_id), (message_id, kind, now())])


async def claim_offer_shown(user_id: int, ts: int) -> bool:
    """Отмечает первый показ оффера в воронке. True — только для самого первого показа."""
    changed = await _execute(
        "UPDATE users SET offer_shown_at = ? WHERE user_id = ? AND offer_shown_at IS NULL", (ts, user_id)
    )
    return changed == 1


async def set_blocked(user_id: int, blocked: bool) -> None:
    await _execute("UPDATE users SET blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id))


async def delete_user(user_id: int) -> None:
    """Полностью стирает ученика (используется в /reset для админа)."""
    for table in ("jobs", "payments", "admin_links", "users"):
        await _conn().execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    await _conn().commit()


async def users_for_broadcast(segment: str) -> list[int]:
    """segment: all — всем, paid — купившим, unpaid — не купившим."""
    sql = "SELECT user_id FROM users WHERE blocked = 0"
    if segment == "paid":
        sql += " AND paid_at IS NOT NULL"
    elif segment == "unpaid":
        sql += " AND paid_at IS NULL"
    rows = await _fetchall(sql + " ORDER BY created_at")
    return [r["user_id"] for r in rows]


async def inactive_candidates(threshold: int, limit: int = 50) -> list[dict]:
    """Ученики без активности с момента threshold, которым ещё не показывали оффер."""
    return await _fetchall(
        """SELECT * FROM users
           WHERE blocked = 0 AND paid_at IS NULL AND offer_shown_at IS NULL
             AND inactive_sent = 0 AND last_activity_at <= ?
           ORDER BY last_activity_at LIMIT ?""",
        (threshold, limit),
    )


# ---------------------------------------------------------------- напоминания


async def add_job(user_id: int, kind: str, run_at: int, step: int | None = None) -> None:
    """Планирует напоминание. Если такое напоминание уже есть — переносит его на новое время."""
    await _execute(
        """INSERT INTO jobs (user_id, kind, run_at, step, created_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (user_id, kind) DO UPDATE SET
               run_at = excluded.run_at, step = excluded.step, attempts = 0, created_at = excluded.created_at""",
        (user_id, kind, run_at, step, now()),
    )


async def delete_jobs(user_id: int, kinds: Iterable[str] | None = None) -> None:
    if kinds is None:
        await _execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
        return
    kinds = tuple(kinds)
    placeholders = ", ".join("?" for _ in kinds)
    await _execute(f"DELETE FROM jobs WHERE user_id = ? AND kind IN ({placeholders})", (user_id, *kinds))


async def due_jobs(ts: int, limit: int = 100) -> list[dict]:
    return await _fetchall("SELECT * FROM jobs WHERE run_at <= ? ORDER BY run_at, id LIMIT ?", (ts, limit))


async def user_jobs(user_id: int) -> list[dict]:
    return await _fetchall("SELECT * FROM jobs WHERE user_id = ? ORDER BY run_at", (user_id,))


async def delete_job(job_id: int) -> None:
    await _execute("DELETE FROM jobs WHERE id = ?", (job_id,))


async def reschedule_job(job_id: int, run_at: int, attempts: int | None = None) -> None:
    if attempts is None:
        await _execute("UPDATE jobs SET run_at = ? WHERE id = ?", (run_at, job_id))
    else:
        await _execute("UPDATE jobs SET run_at = ?, attempts = ? WHERE id = ?", (run_at, attempts, job_id))


# ---------------------------------------------------------------- оплаты


async def add_payment(
    user_id: int,
    mode: str,
    amount: int,
    currency: str,
    is_discount: bool,
    status: str,
    charge_id: str | None = None,
) -> int | None:
    """Сохраняет оплату. Возвращает её номер или None, если такая оплата уже была."""
    ts = now()
    cur = await _conn().execute(
        """INSERT OR IGNORE INTO payments
           (user_id, mode, amount, currency, is_discount, telegram_payment_charge_id, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, mode, amount, currency, 1 if is_discount else 0, charge_id, status, ts, ts),
    )
    await _conn().commit()
    payment_id = cur.lastrowid if cur.rowcount == 1 else None
    await cur.close()
    return payment_id


async def get_payment(payment_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM payments WHERE id = ?", (payment_id,))


async def set_payment_status(payment_id: int, status: str, only_if: str | None = None) -> bool:
    """Меняет статус оплаты. Если задан only_if — только когда текущий статус такой. True — если изменили."""
    if only_if is None:
        changed = await _execute(
            "UPDATE payments SET status = ?, updated_at = ? WHERE id = ?", (status, now(), payment_id)
        )
    else:
        changed = await _execute(
            "UPDATE payments SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (status, now(), payment_id, only_if),
        )
    return changed == 1


async def close_pending_payments(user_id: int, status: str = "duplicate") -> None:
    """Закрывает висящие заявки «Я оплатил», когда доступ уже выдан."""
    await _execute(
        "UPDATE payments SET status = ?, updated_at = ? WHERE user_id = ? AND status = 'pending'",
        (status, now(), user_id),
    )


async def pending_payment(user_id: int) -> dict | None:
    return await _fetchone(
        "SELECT * FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (user_id,)
    )


async def last_paid_payment(user_id: int) -> dict | None:
    return await _fetchone(
        "SELECT * FROM payments WHERE user_id = ? AND status = 'paid' ORDER BY id DESC LIMIT 1", (user_id,)
    )


# ---------------------------------------------------------------- переписка админа с учениками


async def link_admin_message(admin_message_id: int, user_id: int) -> None:
    await _execute(
        "INSERT OR REPLACE INTO admin_links (admin_message_id, user_id, created_at) VALUES (?, ?, ?)",
        (admin_message_id, user_id, now()),
    )


async def user_by_admin_message(admin_message_id: int) -> int | None:
    row = await _fetchone("SELECT user_id FROM admin_links WHERE admin_message_id = ?", (admin_message_id,))
    return row["user_id"] if row else None


# ---------------------------------------------------------------- статистика


async def stats() -> dict:
    async def count(where: str = "1") -> int:
        row = await _fetchone(f"SELECT COUNT(*) AS c FROM users WHERE {where}")
        return row["c"] if row else 0

    return {
        "total": await count(),
        "blocked": await count("blocked = 1"),
        "steps": {n: await count(f"max_step_opened >= {n}") for n in range(1, 6)},
        "finished": await count("finished_at IS NOT NULL"),
        "offer_viewed": await count("first_offer_view_at IS NOT NULL"),
        "paid": await count("paid_at IS NOT NULL"),
        "sources": await _fetchall(
            """SELECT COALESCE(NULLIF(source, ''), '') AS source,
                      COUNT(*) AS count,
                      SUM(CASE WHEN paid_at IS NOT NULL THEN 1 ELSE 0 END) AS paid
               FROM users GROUP BY 1 ORDER BY count DESC"""
        ),
        "revenue": await _fetchall(
            "SELECT currency, SUM(amount) AS total FROM payments WHERE status = 'paid' GROUP BY currency"
        ),
    }
