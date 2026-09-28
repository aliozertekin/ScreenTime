"""
SessionManager: turns a stream of "what app is focused right now" events into
accurate, crash-safe, clock-jump-safe usage sessions in the database.

Key correctness properties:
  * Timestamps stored for display are wall-clock (time.time()), but elapsed
    durations are always computed from a monotonic clock. This means an NTP
    step, a manual clock change, or a timezone change cannot inflate or
    corrupt a session's duration -- only real elapsed seconds count.
  * Every heartbeat commits `end_time` for the currently open session, so if
    the daemon is killed with SIGKILL or the machine loses power, at most one
    heartbeat interval (a few seconds) of usage is lost -- never the whole
    session, and never any *previously* closed session.
  * A session is never allowed to silently span a suspend/resume: callers are
    expected to call on_suspend() when the system is about to sleep (wired to
    the logind PrepareForSleep D-Bus signal by the daemon) which closes the
    session immediately, before any suspended time can be misattributed.
  * A session never spans two local calendar days on disk: heartbeat() splits
    it at local midnight, closing one row and opening the next, so day/week/
    month aggregation is a plain GROUP BY over `day`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .db import Database, local_day, day_start_epoch


@dataclass
class FocusInfo:
    """What the window/process detection layer reports as 'currently active'."""
    key: str                      # canonical app identity (e.g. lowercased wm_class/app_id)
    display_name: str
    icon_name: Optional[str] = None
    desktop_file: Optional[str] = None


class SessionManager:
    def __init__(self, db: Database,
                 wall_clock: Callable[[], float] = time.time,
                 mono_clock: Callable[[], float] = time.monotonic):
        self.db = db
        self._wall = wall_clock
        self._mono = mono_clock

        self._current_app_id: Optional[int] = None
        self._current_key: Optional[str] = None
        self._session_id: Optional[int] = None
        self._start_wall: Optional[float] = None
        self._start_mono: Optional[float] = None
        self._is_idle: bool = False

    # ------------------------------------------------------------- helpers
    def _now(self) -> tuple[float, float]:
        return self._wall(), self._mono()

    def _elapsed_wall_end(self, now_mono: float) -> float:
        """Wall-clock end time derived from monotonic elapsed, immune to
        wall-clock jumps (NTP steps, manual date changes, DST, timezone)."""
        assert self._start_wall is not None and self._start_mono is not None
        return self._start_wall + (now_mono - self._start_mono)

    # ------------------------------------------------------------- public
    @property
    def active_app_id(self) -> Optional[int]:
        return self._current_app_id if not self._is_idle else None

    def _close_current(self, end_wall: float, reason: str):
        if self._session_id is not None:
            self.db.close_session(self._session_id, end_wall, reason)
        self._session_id = None
        self._current_app_id = None
        self._current_key = None
        self._start_wall = None
        self._start_mono = None

    def _open_new(self, focus: FocusInfo, start_wall: float, start_mono: float):
        app = self.db.get_or_create_app(focus.key, focus.display_name, focus.icon_name, focus.desktop_file)
        if app.excluded:
            self._current_app_id = None
            self._current_key = focus.key
            self._session_id = None
            self._start_wall = None
            self._start_mono = None
            return
        self._current_app_id = app.id
        self._current_key = focus.key
        self._start_wall = start_wall
        self._start_mono = start_mono
        self._session_id = self.db.open_session(app.id, start_wall)

    def on_focus_change(self, focus: Optional[FocusInfo]):
        """Called whenever the detection layer observes a different focused
        app than before (or None, meaning nothing trackable is focused, e.g.
        the desktop shell / no window / an excluded app)."""
        now_wall, now_mono = self._now()
        new_key = focus.key if focus else None
        if new_key == self._current_key and not self._is_idle:
            return  # no actual change
        self._is_idle = False
        end_wall = self._elapsed_wall_end(now_mono) if self._session_id is not None else now_wall
        self._close_current(end_wall, "focus_change")
        if focus is not None:
            self._open_new(focus, now_wall, now_mono)
        else:
            self._current_key = None

    def on_idle_start(self):
        """User has been idle past the configured threshold: stop counting
        time immediately. Note the caller (idle detector) should pass the
        timestamp of the *last real input*, not 'now', via on_idle_start_at,
        so the idle gap itself is never counted as usage."""
        self.on_idle_start_at(self._now()[0])

    def on_idle_start_at(self, last_input_wall: float):
        if self._session_id is None or self._is_idle:
            self._is_idle = True
            return
        # Close the session at the moment the user actually went idle, not now.
        end_wall = min(last_input_wall, self._elapsed_wall_end(self._mono()))
        end_wall = max(end_wall, self._start_wall)
        self._close_current(end_wall, "idle")
        self._is_idle = True

    def on_idle_end(self, focus: Optional[FocusInfo]):
        """User is active again. Caller supplies whatever is currently
        focused so a new session can start right away."""
        self._is_idle = False
        self._current_key = None  # force on_focus_change to treat this as new
        self.on_focus_change(focus)

    def on_suspend(self):
        """System is about to sleep. Close any open session immediately so
        suspended wall-clock time is never attributed to usage."""
        if self._session_id is not None:
            now_wall, now_mono = self._now()
            end_wall = self._elapsed_wall_end(now_mono)
            self._close_current(end_wall, "suspend")

    def on_resume(self, focus: Optional[FocusInfo]):
        self._current_key = None
        self.on_focus_change(focus)

    def heartbeat(self):
        """Call periodically (e.g. every 5-10s) from the daemon loop. Persists
        partial progress (crash safety) and splits the session if local
        midnight was crossed since the last heartbeat."""
        if self._session_id is None or self._is_idle:
            return
        now_wall, now_mono = self._now()
        end_wall = self._elapsed_wall_end(now_mono)

        session_day = local_day(self._start_wall)
        today = local_day(end_wall)
        if today != session_day:
            # Use the exact same instant (local midnight) as both the close
            # time of the old day's row and the open time of the new day's
            # row, so no real elapsed time falls into an unaccounted gap.
            boundary = day_start_epoch(today)
            self._session_id = self.db.split_session_at_midnight(self._session_id, boundary, boundary)
            # New session started exactly at the midnight boundary; rebase our
            # monotonic reference point so elapsed-time math stays correct.
            self._start_wall = boundary
            self._start_mono = now_mono - (end_wall - boundary)
            end_wall = self._elapsed_wall_end(now_mono)

        self.db.heartbeat_session(self._session_id, end_wall)

    def shutdown(self, reason: str = "daemon_stop"):
        """Call on clean daemon exit (SIGTERM handler, systemd stop)."""
        if self._session_id is not None:
            now_wall, now_mono = self._now()
            end_wall = self._elapsed_wall_end(now_mono)
            self._close_current(end_wall, reason)
