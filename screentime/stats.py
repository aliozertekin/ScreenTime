"""
Read-only query layer over the database, used exclusively by the GUI. Never
writes sessions -- all writes to `sessions`/`daily_totals` happen in the
daemon via SessionManager/Database. This keeps "the GUI can be closed
without affecting tracking" trivially true: it holds no state the tracker
depends on.

All aggregate queries read from `daily_totals` (fast, pre-aggregated) rather
than scanning `sessions`, except session-level views (session list, longest
session, average session length) which necessarily read `sessions` directly.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from .db import Database


def _today() -> datetime.date:
    return datetime.date.today()


def today_str() -> str:
    return _today().isoformat()


def yesterday_str() -> str:
    return (_today() - datetime.timedelta(days=1)).isoformat()


def week_start_str(d: datetime.date | None = None) -> str:
    d = d or _today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()  # Monday


def month_start_str(d: datetime.date | None = None) -> str:
    d = d or _today()
    return d.replace(day=1).isoformat()


@dataclass
class AppUsage:
    app_id: int
    key: str
    display_name: str
    icon_name: str | None
    seconds: int
    session_count: int
    percent: float = 0.0


@dataclass
class RangeSummary:
    total_seconds: int
    apps: list[AppUsage]


def usage_in_range(db: Database, start_day: str, end_day: str, include_excluded: bool = False) -> RangeSummary:
    q = (
        "SELECT a.id as app_id, a.key, a.display_name, a.icon_name, "
        "SUM(dt.seconds) as seconds, SUM(dt.session_count) as session_count "
        "FROM daily_totals dt JOIN apps a ON a.id = dt.app_id "
        "WHERE dt.day >= ? AND dt.day <= ?"
    )
    params: list = [start_day, end_day]
    if not include_excluded:
        q += " AND a.excluded = 0"
    q += " GROUP BY a.id ORDER BY seconds DESC"
    rows = db.conn.execute(q, params).fetchall()
    total = sum(r["seconds"] for r in rows) or 0
    apps = []
    for r in rows:
        pct = (r["seconds"] / total * 100.0) if total else 0.0
        apps.append(AppUsage(
            app_id=r["app_id"], key=r["key"], display_name=r["display_name"], icon_name=r["icon_name"],
            seconds=r["seconds"], session_count=r["session_count"], percent=pct,
        ))
    return RangeSummary(total_seconds=total, apps=apps)


def today_summary(db: Database) -> RangeSummary:
    t = today_str()
    return usage_in_range(db, t, t)


def yesterday_summary(db: Database) -> RangeSummary:
    y = yesterday_str()
    return usage_in_range(db, y, y)


def week_summary(db: Database) -> RangeSummary:
    return usage_in_range(db, week_start_str(), today_str())


def last_7_days_summary(db: Database) -> RangeSummary:
    start = (_today() - datetime.timedelta(days=6)).isoformat()
    return usage_in_range(db, start, today_str())


def month_summary(db: Database) -> RangeSummary:
    return usage_in_range(db, month_start_str(), today_str())


def all_time_summary(db: Database, include_excluded: bool = False) -> RangeSummary:
    return usage_in_range(db, "0000-01-01", "9999-12-31", include_excluded)


def daily_breakdown(db: Database, app_id: int, start_day: str, end_day: str) -> list[tuple[str, int]]:
    rows = db.conn.execute(
        "SELECT day, seconds FROM daily_totals WHERE app_id = ? AND day >= ? AND day <= ? ORDER BY day",
        (app_id, start_day, end_day),
    ).fetchall()
    return [(r["day"], r["seconds"]) for r in rows]


def daily_totals_all_apps(db: Database, start_day: str, end_day: str) -> list[tuple[str, int]]:
    """Total (all apps combined) usage per day, for the history bar chart."""
    rows = db.conn.execute(
        "SELECT dt.day as day, SUM(dt.seconds) as seconds FROM daily_totals dt "
        "JOIN apps a ON a.id = dt.app_id WHERE a.excluded = 0 AND dt.day >= ? AND dt.day <= ? "
        "GROUP BY dt.day ORDER BY dt.day",
        (start_day, end_day),
    ).fetchall()
    return [(r["day"], r["seconds"]) for r in rows]


@dataclass
class AppDetail:
    app_id: int
    display_name: str
    today_seconds: int
    last7_seconds: int
    month_seconds: int
    all_time_seconds: int
    session_count: int
    avg_session_seconds: float
    longest_session_seconds: int
    daily: list[tuple[str, int]]
    sessions_today: list[tuple[float, float, int]]  # (start, end, duration)


def app_detail(db: Database, app_id: int) -> AppDetail:
    app = db.get_app(app_id)
    t = today_str()

    def total_for(start, end):
        rows = db.conn.execute(
            "SELECT SUM(seconds) s FROM daily_totals WHERE app_id=? AND day>=? AND day<=?",
            (app_id, start, end),
        ).fetchone()
        return rows["s"] or 0

    today_s = total_for(t, t)
    last7_s = total_for((_today() - datetime.timedelta(days=6)).isoformat(), t)
    month_s = total_for(month_start_str(), t)
    all_s = total_for("0000-01-01", "9999-12-31")

    sess_rows = db.conn.execute(
        "SELECT start_time, COALESCE(end_time, last_heartbeat, start_time) as end_time "
        "FROM sessions WHERE app_id = ?", (app_id,),
    ).fetchall()
    durations = [max(0, r["end_time"] - r["start_time"]) for r in sess_rows]
    session_count = len(durations)
    avg = (sum(durations) / session_count) if session_count else 0.0
    longest = max(durations) if durations else 0

    daily = daily_breakdown(db, app_id, month_start_str(), t)

    today_rows = db.conn.execute(
        "SELECT start_time, COALESCE(end_time, last_heartbeat, start_time) as end_time "
        "FROM sessions WHERE app_id = ? AND day = ? ORDER BY start_time",
        (app_id, t),
    ).fetchall()
    sessions_today = [(r["start_time"], r["end_time"], r["end_time"] - r["start_time"]) for r in today_rows]

    return AppDetail(
        app_id=app_id, display_name=app.display_name if app else "?",
        today_seconds=today_s, last7_seconds=last7_s, month_seconds=month_s, all_time_seconds=all_s,
        session_count=session_count, avg_session_seconds=avg, longest_session_seconds=longest,
        daily=daily, sessions_today=sessions_today,
    )


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s" if (m < 10 and s) else f"{m}m"
    return f"{s}s"
