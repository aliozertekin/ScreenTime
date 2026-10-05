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


# ------------------------------------------------------------------ dashboard
PERIODS = (1, 7, 30)                     # days: Today, Last 7 days, Last 30 days
TREND_DAYS = {1: 7, 7: 7, 30: 30}        # "Today" still shows the last week as context


@dataclass
class Comparison:
    current: int
    previous: int

    @property
    def delta(self) -> int:
        return self.current - self.previous

    @property
    def percent(self) -> float | None:
        """Change relative to the previous period; None when there is nothing to compare with."""
        return None if self.previous <= 0 else self.delta / self.previous * 100.0


def describe_change(c: Comparison, previous_label: str) -> str:
    """One plain sentence ("18% more than yesterday"). Wording, not just colour, carries the meaning."""
    if c.previous <= 0:
        return f"No usage recorded for {previous_label} to compare with"
    pct = c.percent
    if abs(c.delta) < 60 or abs(pct) < 1:
        return f"About the same as {previous_label}"
    word = "more" if c.delta > 0 else "less"
    return f"{abs(pct):.0f}% {word} than {previous_label} ({format_duration(abs(c.delta))} {word})"


@dataclass
class DashboardData:
    period_days: int
    start: str
    end: str
    summary: RangeSummary
    comparison: Comparison
    previous_label: str
    daily: list                          # [(day 'YYYY-MM-DD', seconds)] zero-filled, oldest first
    average_seconds: int                 # per day over the period
    busiest_day: tuple | None            # (day, seconds) within the trend window, or None

    @property
    def has_data(self) -> bool:
        return self.summary.total_seconds > 0 or any(s for _d, s in self.daily)


def fill_days(rows: list, start: datetime.date, end: datetime.date) -> list:
    """Every day from start to end inclusive, with 0 for days that have no row."""
    have = dict(rows)
    out, d = [], start
    while d <= end:
        out.append((d.isoformat(), int(have.get(d.isoformat(), 0))))
        d += datetime.timedelta(days=1)
    return out


def dashboard_data(db: Database, period_days: int) -> DashboardData:
    """Everything the dashboard shows, from the existing aggregate queries (never the raw session log):
    a handful of GROUP BYs over `daily_totals`, so it stays fast with years of history."""
    if period_days not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    today = _today()
    start = today - datetime.timedelta(days=period_days - 1)
    prev_end = start - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=period_days - 1)
    summary = usage_in_range(db, start.isoformat(), today.isoformat())
    prev_total = usage_in_range(db, prev_start.isoformat(), prev_end.isoformat()).total_seconds
    trend_days = TREND_DAYS[period_days]
    t_start = today - datetime.timedelta(days=trend_days - 1)
    daily = fill_days(daily_totals_all_apps(db, t_start.isoformat(), today.isoformat()), t_start, today)
    busiest = max(daily, key=lambda d: d[1]) if any(s for _d, s in daily) else None
    label = {1: "yesterday", 7: "the previous 7 days", 30: "the previous 30 days"}[period_days]
    return DashboardData(
        period_days=period_days, start=start.isoformat(), end=today.isoformat(), summary=summary,
        comparison=Comparison(summary.total_seconds, prev_total), previous_label=label, daily=daily,
        average_seconds=summary.total_seconds // period_days, busiest_day=busiest)


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s" if (m < 10 and s) else f"{m}m"
    return f"{s}s"
