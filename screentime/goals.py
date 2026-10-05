"""Optional screen-time goals, and the notifications that go with them.

Three different numbers, kept apart everywhere (storage, evaluation, UI, notifications):

* **usage**     -- what has actually been tracked today (from `daily_totals`).
* **goal**      -- the limit the user chose, e.g. "Firefox: 2h".
* **threshold** -- a percentage of the goal (default 80%) at which a *heads-up* is shown before the goal
                   itself is reached. 80% of a 2h goal is 1h 36m.

Everything is computed locally from data the daemon already tracks. Goals never block, close or throttle
applications; the only effect is an optional desktop notification.

Storage: one JSON document in the existing `settings` table (key `goals_v1`), so goals persist across
restarts and the GUI and the daemon read the same value without any new IPC or schema change. Which
notifications were already shown today lives in `goals_notified_v1`, so a restart never repeats one.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from . import stats
from .db import Database
from .notifications import Notifier

log = logging.getLogger("screentime.goals")

SETTING_KEY = "goals_v1"
NOTIFIED_KEY = "goals_notified_v1"
MAX_GOAL_SECONDS = 24 * 3600
MIN_WARN, MAX_WARN, DEFAULT_WARN = 1, 99, 80

OK, WARNING, REACHED = "ok", "warning", "reached"


@dataclass
class Goals:
    enabled: bool = False                 # master switch: with it off nothing is evaluated or shown
    notify: bool = True                   # desktop notifications (goals still show in the dashboard)
    warn_percent: int = DEFAULT_WARN
    daily_total_seconds: int = 0          # 0 = no overall goal
    app_seconds: dict = field(default_factory=dict)   # app key -> daily goal in seconds

    def has_any(self) -> bool:
        return self.daily_total_seconds > 0 or any(v > 0 for v in self.app_seconds.values())


def _clamp_seconds(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_GOAL_SECONDS, n))


def normalize(goals: Goals) -> Goals:
    goals.warn_percent = max(MIN_WARN, min(MAX_WARN, int(goals.warn_percent)))
    goals.daily_total_seconds = _clamp_seconds(goals.daily_total_seconds)
    goals.app_seconds = {str(k): _clamp_seconds(v) for k, v in goals.app_seconds.items()
                         if str(k) and _clamp_seconds(v) > 0}
    return goals


def load(db: Database) -> Goals:
    """Stored goals, or defaults. A damaged value degrades to defaults and is never written back."""
    raw = db.get_setting(SETTING_KEY)
    if not raw:
        return Goals()
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return normalize(Goals(
            enabled=bool(d.get("enabled", False)), notify=bool(d.get("notify", True)),
            warn_percent=d.get("warn_percent", DEFAULT_WARN) if isinstance(d.get("warn_percent"), int) else DEFAULT_WARN,
            daily_total_seconds=d.get("daily_total_seconds", 0),
            app_seconds=d.get("app_seconds", {}) if isinstance(d.get("app_seconds"), dict) else {}))
    except (ValueError, TypeError) as e:
        log.warning("ignoring unreadable goals setting: %s", e)
        return Goals()


def save(db: Database, goals: Goals) -> None:
    goals = normalize(goals)
    db.set_setting(SETTING_KEY, json.dumps({
        "version": 1, "enabled": goals.enabled, "notify": goals.notify, "warn_percent": goals.warn_percent,
        "daily_total_seconds": goals.daily_total_seconds, "app_seconds": goals.app_seconds},
        sort_keys=True, separators=(",", ":")))


@dataclass
class GoalStatus:
    scope: str            # "total" | "app"
    key: str              # "" for the total goal, else the app key
    name: str
    used: int             # usage today, seconds
    limit: int            # the goal, seconds
    warn_at: int          # usage at which the heads-up fires, seconds
    state: str            # ok | warning | reached

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def warn_threshold(limit: int, percent: int) -> int:
    return min(limit, max(1, math.ceil(limit * percent / 100)))


def _state(used: int, limit: int, warn_at: int) -> str:
    if used >= limit:
        return REACHED
    return WARNING if used >= warn_at else OK


def evaluate(db: Database, goals: Optional[Goals] = None, day: Optional[str] = None) -> list[GoalStatus]:
    """Status of every configured goal for `day` (default today). Empty when goals are off."""
    goals = goals or load(db)
    if not goals.enabled or not goals.has_any():
        return []
    day = day or stats.today_str()
    today = stats.usage_in_range(db, day, day)           # same numbers the dashboard shows
    by_key = {a.key: a for a in today.apps}
    out: list[GoalStatus] = []
    if goals.daily_total_seconds > 0:
        lim = goals.daily_total_seconds
        w = warn_threshold(lim, goals.warn_percent)
        out.append(GoalStatus("total", "", "All applications", today.total_seconds, lim, w,
                              _state(today.total_seconds, lim, w)))
    for key, lim in sorted(goals.app_seconds.items()):
        app = by_key.get(key)
        known = db.get_app_by_key(key)
        if known is not None and known.excluded:
            continue                                        # excluded apps are never tracked or judged
        used = app.seconds if app else 0
        name = app.display_name if app else (known.display_name if known else key)
        w = warn_threshold(lim, goals.warn_percent)
        out.append(GoalStatus("app", key, name, used, lim, w, _state(used, lim, w)))
    return out


# --------------------------------------------------------------- notifications
def _event_id(s: GoalStatus, state: str) -> str:
    # The limit is part of the id: raising a goal and then hitting the new one is a new event.
    return f"{s.scope}:{s.key}:{state}:{s.limit}"


def _message(s: GoalStatus, warn_percent: int) -> tuple[str, str]:
    used, limit = stats.format_duration(s.used), stats.format_duration(s.limit)
    what = "Your screen time" if s.scope == "total" else s.name
    if s.state == REACHED:
        return ("Daily goal reached", f"{what}: {used} today, goal {limit}.")
    return (f"{warn_percent}% of your daily goal", f"{what}: {used} of {limit} today.")


class GoalNotifier:
    """Called periodically by the daemon. Sends each notification at most once per day and goal."""

    def __init__(self, db: Database, notifier: Notifier, today=stats.today_str):
        self.db, self.notifier, self._today = db, notifier, today

    def _load_sent(self, day: str) -> set:
        try:
            d = json.loads(self.db.get_setting(NOTIFIED_KEY) or "{}")
            if d.get("day") == day and isinstance(d.get("sent"), list):
                return set(map(str, d["sent"]))
        except (ValueError, AttributeError):
            pass
        return set()

    def _save_sent(self, day: str, sent: set) -> None:
        self.db.set_setting(NOTIFIED_KEY, json.dumps({"day": day, "sent": sorted(sent)}, separators=(",", ":")))

    def check(self) -> int:
        """Evaluate goals and notify for new threshold crossings. Returns how many were sent."""
        goals = load(self.db)
        if not (goals.enabled and goals.notify and goals.has_any()):
            return 0
        day = self._today()
        statuses = [s for s in evaluate(self.db, goals, day) if s.state != OK]
        if not statuses:
            return 0
        sent = self._load_sent(day)
        n, changed = 0, False
        for s in statuses:
            eid = _event_id(s, s.state)
            if eid in sent:
                continue
            title, body = _message(s, goals.warn_percent)
            if not self.notifier.send(title, body):
                continue                                    # not delivered: try again at the next check
            sent.add(eid)
            if s.state == REACHED:                          # a skipped heads-up must not fire after the limit
                sent.add(_event_id(s, WARNING))
            n += 1
            changed = True
        if changed:
            self._save_sent(day, sent)
        return n
