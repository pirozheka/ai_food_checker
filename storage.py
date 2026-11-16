"""SQLite-дневник приёмов пищи."""
import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    ts       TEXT    NOT NULL,          -- UTC ISO8601
    dish     TEXT    NOT NULL,
    weight_g REAL    NOT NULL DEFAULT 0,
    kcal     REAL    NOT NULL DEFAULT 0,
    protein_g REAL   NOT NULL DEFAULT 0,
    fat_g    REAL    NOT NULL DEFAULT 0,
    carbs_g  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS meals_user_ts ON meals (user_id, ts);

-- Какие недельные отчёты уже отправлены: защита от повтора после перезапуска.
CREATE TABLE IF NOT EXISTS weekly_reports (
    user_id    INTEGER NOT NULL,
    week_start TEXT    NOT NULL,        -- локальная дата понедельника, YYYY-MM-DD
    sent_ts    TEXT    NOT NULL,
    PRIMARY KEY (user_id, week_start)
);
"""

_UTC = ZoneInfo("UTC")


@dataclass
class DayTotals:
    count: int = 0
    kcal: float = 0.0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carbs_g: float = 0.0


@dataclass
class WeekReport:
    """Неделя понедельник–воскресенье в локальной зоне."""
    monday: date
    sunday: date
    # По одной записи на каждый из семи дней, включая пустые.
    days: list[tuple[date, DayTotals]] = field(default_factory=list)
    totals: DayTotals = field(default_factory=DayTotals)

    @property
    def active_days(self) -> int:
        """Дней, в которые пользователь что-то записал."""
        return sum(1 for _, t in self.days if t.count)

    @property
    def avg_kcal(self) -> float:
        """Среднее по дням с записями — по пустым усреднять бессмысленно."""
        return self.totals.kcal / self.active_days if self.active_days else 0.0


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def today_local() -> date:
    """Сегодняшняя дата в часовом поясе пользователя."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _bounds_utc(start_day: date, days: int = 1) -> tuple[str, str]:
    """Границы отрезка локальных суток, выраженные в UTC ISO."""
    tz = ZoneInfo(settings.timezone)
    start_local = datetime(start_day.year, start_day.month, start_day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=days)
    return (
        start_local.astimezone(_UTC).isoformat(),
        end_local.astimezone(_UTC).isoformat(),
    )


def _local_date(ts: str) -> date:
    """UTC ISO из базы → локальная дата."""
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(settings.timezone)).date()


def _totals(rows) -> DayTotals:
    return DayTotals(
        count=len(rows),
        kcal=sum(r["kcal"] for r in rows),
        protein_g=sum(r["protein_g"] for r in rows),
        fat_g=sum(r["fat_g"] for r in rows),
        carbs_g=sum(r["carbs_g"] for r in rows),
    )


def _add(user_id: int, dish: str, weight_g: float, kcal: float,
         protein_g: float, fat_g: float, carbs_g: float,
         day: date | None = None) -> int:
    if day is None or day == today_local():
        # Сегодняшняя запись — реальное время, чтобы порядок приёмов пищи был честным.
        ts = datetime.now(_UTC).isoformat()
    else:
        # Задним числом: ставим полдень того дня, чтобы запись гарантированно
        # попала в нужные локальные сутки при любом часовом поясе.
        tz = ZoneInfo(settings.timezone)
        ts = datetime(day.year, day.month, day.day, 12, tzinfo=tz).astimezone(_UTC).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO meals (user_id, ts, dish, weight_g, kcal, protein_g, fat_g, carbs_g)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, ts, dish, weight_g, kcal, protein_g, fat_g, carbs_g),
        )
        return int(cur.lastrowid)


def _delete(user_id: int, meal_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM meals WHERE id = ? AND user_id = ?", (meal_id, user_id)
        )
        return cur.rowcount > 0


def _delete_last(user_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, dish FROM meals WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM meals WHERE id = ?", (row["id"],))
        return row["dish"]


def _day(user_id: int, day: date) -> tuple[DayTotals, list[sqlite3.Row]]:
    start, end = _bounds_utc(day)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, dish, weight_g, kcal, protein_g, fat_g, carbs_g FROM meals"
            " WHERE user_id = ? AND ts >= ? AND ts < ? ORDER BY id",
            (user_id, start, end),
        ).fetchall()
    return _totals(rows), rows


def _clear_day(user_id: int, day: date) -> int:
    start, end = _bounds_utc(day)
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM meals WHERE user_id = ? AND ts >= ? AND ts < ?",
            (user_id, start, end),
        )
        return cur.rowcount


def _week(user_id: int, monday: date) -> WeekReport:
    start, end = _bounds_utc(monday, days=7)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, dish, kcal, protein_g, fat_g, carbs_g FROM meals"
            " WHERE user_id = ? AND ts >= ? AND ts < ? ORDER BY id",
            (user_id, start, end),
        ).fetchall()

    by_day: dict[date, list[sqlite3.Row]] = {}
    for row in rows:
        by_day.setdefault(_local_date(row["ts"]), []).append(row)

    days = [
        (monday + timedelta(days=i), _totals(by_day.get(monday + timedelta(days=i), [])))
        for i in range(7)
    ]
    return WeekReport(
        monday=monday,
        sunday=monday + timedelta(days=6),
        days=days,
        totals=_totals(rows),
    )


def _week_dishes(user_id: int, monday: date, limit: int = 40) -> list[str]:
    """Названия блюд за неделю — сырьё для рекомендаций модели."""
    start, end = _bounds_utc(monday, days=7)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT dish FROM meals WHERE user_id = ? AND ts >= ? AND ts < ?"
            " ORDER BY id DESC LIMIT ?",
            (user_id, start, end, limit),
        ).fetchall()
    return [r["dish"] for r in rows]


def _users_with_meals(monday: date) -> list[int]:
    """Кому есть смысл слать недельный отчёт."""
    start, end = _bounds_utc(monday, days=7)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM meals WHERE ts >= ? AND ts < ?", (start, end)
        ).fetchall()
    return [r["user_id"] for r in rows]


def _mark_report_sent(user_id: int, monday: date) -> bool:
    """True — отчёт помечен как отправленный сейчас; False — уже был раньше."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO weekly_reports (user_id, week_start, sent_ts)"
            " VALUES (?, ?, ?)",
            (user_id, monday.isoformat(), datetime.now(_UTC).isoformat()),
        )
        return cur.rowcount > 0


# Асинхронные обёртки: sqlite синхронный, уводим его из event loop.
async def init() -> None:
    await asyncio.to_thread(_init)


async def add_meal(user_id: int, dish: str, weight_g: float, kcal: float,
                   protein_g: float, fat_g: float, carbs_g: float,
                   day: date | None = None) -> int:
    return await asyncio.to_thread(
        _add, user_id, dish, weight_g, kcal, protein_g, fat_g, carbs_g, day
    )


async def delete_meal(user_id: int, meal_id: int) -> bool:
    return await asyncio.to_thread(_delete, user_id, meal_id)


async def delete_last(user_id: int) -> str | None:
    return await asyncio.to_thread(_delete_last, user_id)


async def day(user_id: int, target: date) -> tuple[DayTotals, list[sqlite3.Row]]:
    return await asyncio.to_thread(_day, user_id, target)


async def today(user_id: int) -> tuple[DayTotals, list[sqlite3.Row]]:
    return await asyncio.to_thread(_day, user_id, today_local())


async def clear_today(user_id: int) -> int:
    return await asyncio.to_thread(_clear_day, user_id, today_local())


async def week(user_id: int, monday: date) -> WeekReport:
    return await asyncio.to_thread(_week, user_id, monday)


async def week_dishes(user_id: int, monday: date) -> list[str]:
    return await asyncio.to_thread(_week_dishes, user_id, monday)


async def users_with_meals(monday: date) -> list[int]:
    return await asyncio.to_thread(_users_with_meals, monday)


async def mark_report_sent(user_id: int, monday: date) -> bool:
    return await asyncio.to_thread(_mark_report_sent, user_id, monday)
