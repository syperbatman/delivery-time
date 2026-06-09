"""
Хранилище отчётов на SQLite.

Ключевое отличие от старой версии (где всё жило в словаре в памяти):
- данные пишутся на диск и переживают перезапуск;
- одна строка = один день одного пользователя (PRIMARY KEY user_id+report_date),
  поэтому повторная отправка того же отчёта обновляет строку, а не задваивает;
- среднее можно пересчитать в любой момент и за любое окно (тут — неделя Пн–Вс).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from parser import ParsedReport

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "delivery.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    user_id          INTEGER NOT NULL,
    report_date      TEXT    NOT NULL,   -- 'YYYY-MM-DD'
    delivery_sec     INTEGER,            -- NULL, если ∅ (смена после 23:00)
    start_sec        INTEGER,
    delivered_orders INTEGER DEFAULT 0,
    orders_before_23 INTEGER DEFAULT 0,
    orders_all       INTEGER DEFAULT 0,
    earnings         REAL    DEFAULT 0,
    raw_filename     TEXT,
    parsed_at        TEXT,
    PRIMARY KEY (user_id, report_date)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id    INTEGER PRIMARY KEY,
    until       TEXT NOT NULL,    -- 'YYYY-MM-DD', включительно последний активный день
    updated_at  TEXT,
    note        TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    # Новое соединение на операцию — безопасно при многопоточном telebot
    # (каждый апдейт обрабатывается в своём потоке).
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")     # параллельные читатели + один писатель
    conn.execute("PRAGMA busy_timeout=5000;")    # ждать до 5с вместо 'database is locked'
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def upsert_report(user_id: int, r: ParsedReport) -> str:
    """
    Сохранить/обновить отчёт за день. Возвращает 'new' или 'updated'
    (для дедупа: если такой день уже был — это 'updated').
    """
    if not r.report_date:
        raise ValueError("в отчёте не найдена дата")

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM reports WHERE user_id=? AND report_date=?",
            (user_id, r.report_date),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO reports (user_id, report_date, delivery_sec, start_sec,
                delivered_orders, orders_before_23, orders_all,
                earnings, raw_filename, parsed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, report_date) DO UPDATE SET
                delivery_sec=excluded.delivery_sec,
                start_sec=excluded.start_sec,
                delivered_orders=excluded.delivered_orders,
                orders_before_23=excluded.orders_before_23,
                orders_all=excluded.orders_all,
                earnings=excluded.earnings,
                raw_filename=excluded.raw_filename,
                parsed_at=excluded.parsed_at
            """,
            (
                user_id, r.report_date, r.delivery_sec, r.start_sec,
                r.delivered_orders, r.orders_before_23, r.orders_all,
                r.earnings, r.raw_filename,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return "updated" if existing else "new"


def week_bounds(ref: date) -> tuple[str, str]:
    """Границы ISO-недели (понедельник..воскресенье) для даты ref, как 'YYYY-MM-DD'."""
    monday = ref - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


@dataclass
class WeeklyStats:
    week_start: str
    week_end: str
    days_total: int                       # сколько дней с отчётами на неделе
    days_with_delivery: int               # из них с непустым временем доставки
    avg_delivery_sec: Optional[int]       # средневзвешенное по заказам; None если нет данных
    avg_start_sec: Optional[int]
    total_orders: int
    total_earnings: float
    days: list                            # список sqlite3.Row по дням (для разбивки)


def _aggregate(rows, start: str, end: str) -> WeeklyStats:
    """
    Свести строки одной недели в WeeklyStats.

    Среднее время доставки = средневзвешенное дневных средних по числу заказов,
    учитываются ТОЛЬКО дни с непустым delivery_sec (∅-дни исключены, а не обнулены).
    Вес = orders_before_23 (заказы, к которым относится время доставки),
    с откатом на delivered_orders, затем на 1. Время выезда — по всем дням.
    """
    num = den = 0          # взвешенное среднее доставки
    snum = sden = 0        # взвешенное среднее выезда
    total_orders = 0
    total_earnings = 0.0
    days_with_delivery = 0

    for row in rows:
        total_orders += row["orders_all"] or 0
        total_earnings += row["earnings"] or 0.0

        if row["delivery_sec"] is not None:
            w = row["orders_before_23"] or row["delivered_orders"] or 1
            num += row["delivery_sec"] * w
            den += w
            days_with_delivery += 1

        if row["start_sec"] is not None:
            sw = row["orders_all"] or row["delivered_orders"] or 1
            snum += row["start_sec"] * sw
            sden += sw

    return WeeklyStats(
        week_start=start,
        week_end=end,
        days_total=len(rows),
        days_with_delivery=days_with_delivery,
        avg_delivery_sec=round(num / den) if den else None,
        avg_start_sec=round(snum / sden) if sden else None,
        total_orders=total_orders,
        total_earnings=total_earnings,
        days=list(rows),
    )


def weekly_stats(user_id: int, ref: Optional[date] = None) -> WeeklyStats:
    """Статистика за одну неделю (Пн–Вс), содержащую дату ref (по умолчанию сегодня)."""
    ref = ref or date.today()
    start, end = week_bounds(ref)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE user_id=? AND report_date BETWEEN ? AND ? "
            "ORDER BY report_date",
            (user_id, start, end),
        ).fetchall()
    return _aggregate(rows, start, end)


def all_weeks_stats(user_id: int) -> list[WeeklyStats]:
    """
    Разбивка по ВСЕМ календарным неделям, в которых есть отчёты пользователя.
    Возвращает список WeeklyStats в хронологическом порядке (от ранней к поздней).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE user_id=? ORDER BY report_date", (user_id,)
        ).fetchall()

    groups: dict[tuple, list] = {}
    for row in rows:
        bounds = week_bounds(date.fromisoformat(row["report_date"]))
        groups.setdefault(bounds, []).append(row)

    return [_aggregate(rs, s, e) for (s, e), rs in sorted(groups.items())]


def delete_user_data(user_id: int) -> int:
    """Удалить все отчёты пользователя (для /reset). Возвращает число удалённых строк."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM reports WHERE user_id=?", (user_id,))
        return cur.rowcount


def admin_stats() -> dict:
    """Сводка для владельца: пользователи/отчёты всего и за текущую неделю."""
    wk_start, wk_end = week_bounds(date.today())
    today = date.today().isoformat()
    with get_conn() as conn:
        total_users = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM reports").fetchone()[0]
        total_reports = conn.execute(
            "SELECT COUNT(*) FROM reports").fetchone()[0]
        week_users = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM reports WHERE report_date BETWEEN ? AND ?",
            (wk_start, wk_end)).fetchone()[0]
        week_reports = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE report_date BETWEEN ? AND ?",
            (wk_start, wk_end)).fetchone()[0]
        last_activity = conn.execute(
            "SELECT MAX(parsed_at) FROM reports").fetchone()[0]
        active_subs = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE until >= ?", (today,)).fetchone()[0]
    return {
        "total_users": total_users,
        "total_reports": total_reports,
        "week_users": week_users,
        "week_reports": week_reports,
        "week_start": wk_start,
        "week_end": wk_end,
        "last_activity": last_activity,
        "active_subs": active_subs,
    }


# ───────────────────────────── подписки ─────────────────────────────
def subscription_until(user_id: int) -> Optional[str]:
    """Дата окончания подписки 'YYYY-MM-DD' или None, если подписки нет."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT until FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    return row["until"] if row else None


def is_subscribed(user_id: int) -> bool:
    """Активна ли подписка сегодня (ISO-даты сравниваются как строки корректно)."""
    until = subscription_until(user_id)
    return bool(until and until >= date.today().isoformat())


def grant_subscription(user_id: int, days: int = 30, note: str = "") -> str:
    """Выдать/продлить подписку на days дней. Если ещё активна — продлеваем от её конца."""
    today = date.today()
    cur = subscription_until(user_id)
    base = today
    if cur:
        cur_d = date.fromisoformat(cur)
        if cur_d > today:
            base = cur_d
    new_until = (base + timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id, until, updated_at, note) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET until=excluded.until, "
            "updated_at=excluded.updated_at, note=excluded.note",
            (user_id, new_until, datetime.now().isoformat(timespec="seconds"), note),
        )
    return new_until


def revoke_subscription(user_id: int) -> bool:
    """Закрыть подписку немедленно (until = вчера). Возвращает True, если запись была."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE subscriptions SET until=?, updated_at=? WHERE user_id=?",
            (yesterday, datetime.now().isoformat(timespec="seconds"), user_id),
        )
        return cur.rowcount > 0
