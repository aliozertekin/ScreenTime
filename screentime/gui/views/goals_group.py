"""Settings -> Goals: optional daily limits, a warning threshold, and notifications (all local)."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ... import goals as goals_mod
from ... import stats
from ...db import Database

STEP_MINUTES = 15
DEFAULT_APP_GOAL_MINUTES = 60


def _fmt_minutes(minutes: int) -> str:
    return "No limit" if minutes <= 0 else stats.format_duration(minutes * 60)


class GoalsGroup(Adw.PreferencesGroup):
    def __init__(self, db: Database):
        super().__init__(
            title="Goals and notifications",
            description="Optional daily limits. ScreenTime only tells you; it never blocks or closes anything. "
                        "Everything is calculated on this computer.")
        self.db = db
        self._syncing = False
        self._rows: list = []
        self._app_group = Adw.PreferencesGroup(
            title="Application goals", description="A daily limit for one application.")
        self._status_group = Adw.PreferencesGroup(title="Today")

        self.enabled_row = Adw.SwitchRow(title="Use goals", subtitle="Turn everything on this page on or off")
        self.enabled_row.connect("notify::active", self._on_enabled)
        self.notify_row = Adw.SwitchRow(
            title="Desktop notifications",
            subtitle="Notify when you pass the warning threshold and when a goal is reached; once per goal per day")
        self.notify_row.connect("notify::active", self._on_notify)
        self.warn_row = Adw.SpinRow.new_with_range(10, 99, 5)
        self.warn_row.set_title("Warning threshold")
        self.warn_row.set_subtitle("Percent of a goal at which you get a heads-up before it is reached")
        self.warn_row.connect("notify::value", self._on_warn)
        self.total_row = Adw.SpinRow.new_with_range(0, 24 * 60, STEP_MINUTES)
        self.total_row.set_title("Daily total goal (minutes)")
        self.total_row.connect("notify::value", self._on_total)
        for r in (self.enabled_row, self.notify_row, self.warn_row, self.total_row):
            self.add(r)

        self.add_row = Adw.ComboRow(title="Add an application goal", subtitle="Pick an application you have used")
        add_btn = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
        add_btn.connect("clicked", self._on_add_app)
        self.add_row.add_suffix(add_btn)
        self._add_btn = add_btn
        self._app_choices: list = []
        self.refresh()

    # the two sub-groups are siblings in the page, built here but appended by the caller
    def extra_groups(self):
        return [self._app_group, self._status_group]

    # -------------------------------------------------------------- state
    def refresh(self):
        g = goals_mod.load(self.db)
        self._syncing = True
        try:
            self.enabled_row.set_active(g.enabled)
            self.notify_row.set_active(g.notify)
            self.warn_row.set_value(g.warn_percent)
            self.total_row.set_value(g.daily_total_seconds // 60)
            self.total_row.set_subtitle(_fmt_minutes(g.daily_total_seconds // 60) + "  (0 = no overall goal)")
            for w in (self.notify_row, self.warn_row, self.total_row, self.add_row, self._app_group):
                w.set_sensitive(g.enabled)
            self._rebuild_apps(g)
            self._rebuild_status(g)
        finally:
            self._syncing = False

    def refresh_status(self):
        """Cheap periodic update: only the 'Today' progress rows, so rows being edited keep focus."""
        self._rebuild_status(goals_mod.load(self.db))

    def _rebuild_apps(self, g):
        for row in self._rows:
            self._app_group.remove(row)
        self._rows.clear()
        if self.add_row.get_parent() is not None:
            self._app_group.remove(self.add_row)
        for key, secs in sorted(g.app_seconds.items()):
            app = self.db.get_app_by_key(key)
            row = Adw.SpinRow.new_with_range(0, 24 * 60, STEP_MINUTES)
            row.set_title(app.display_name if app else key)
            row.set_subtitle("Minutes per day; 0 removes this goal")
            row.set_value(secs // 60)
            row.connect("notify::value", self._on_app_value, key)
            self._app_group.add(row)
            self._rows.append(row)
        self._app_choices = [a for a in self.db.list_apps(include_excluded=False) if a.key not in g.app_seconds]
        self.add_row.set_model(Gtk.StringList.new([a.display_name for a in self._app_choices]))
        self._add_btn.set_sensitive(bool(self._app_choices))
        self.add_row.set_subtitle("Pick an application you have used" if self._app_choices
                                  else "Every tracked application already has a goal")
        self._app_group.add(self.add_row)

    def _rebuild_status(self, g):
        grp = self._status_group
        for row in getattr(self, "_status_rows", []):
            grp.remove(row)
        self._status_rows = []
        statuses = goals_mod.evaluate(self.db, g)
        if not statuses:
            row = Adw.ActionRow(title="No goals to show", subtitle="Turn goals on and set a limit above")
            row.add_css_class("dim-label")
            grp.add(row)
            self._status_rows.append(row)
            return
        for s in statuses:
            label = {"ok": "On track", "warning": "Close to the limit", "reached": "Goal reached"}[s.state]
            row = Adw.ActionRow(title=s.name, subtitle=(
                f"Usage {stats.format_duration(s.used)} of {stats.format_duration(s.limit)} goal"
                f" \u2014 {label}. Warning at {stats.format_duration(s.warn_at)}."))
            bar = Gtk.ProgressBar(fraction=min(1.0, s.fraction), valign=Gtk.Align.CENTER, width_request=120)
            bar.update_property([Gtk.AccessibleProperty.LABEL], [f"{s.name}: {int(s.fraction * 100)} percent of goal"]) \
                if hasattr(bar, "update_property") else None
            row.add_suffix(bar)
            grp.add(row)
            self._status_rows.append(row)

    # ----------------------------------------------------------- handlers
    def _edit(self, fn):
        if self._syncing:
            return
        g = goals_mod.load(self.db)
        fn(g)
        goals_mod.save(self.db, g)
        self.refresh()

    def _on_enabled(self, row, _p):
        self._edit(lambda g: setattr(g, "enabled", row.get_active()))

    def _on_notify(self, row, _p):
        self._edit(lambda g: setattr(g, "notify", row.get_active()))

    def _on_warn(self, row, _p):
        self._edit(lambda g: setattr(g, "warn_percent", int(row.get_value())))

    def _on_total(self, row, _p):
        self._edit(lambda g: setattr(g, "daily_total_seconds", int(row.get_value()) * 60))

    def _on_app_value(self, row, _p, key):
        def fn(g):
            minutes = int(row.get_value())
            if minutes <= 0:
                g.app_seconds.pop(key, None)
            else:
                g.app_seconds[key] = minutes * 60
        self._edit(fn)

    def _on_add_app(self, _btn):
        idx = self.add_row.get_selected()
        if self._syncing or not (0 <= idx < len(self._app_choices)):
            return
        key = self._app_choices[idx].key
        self._edit(lambda g: g.app_seconds.__setitem__(key, DEFAULT_APP_GOAL_MINUTES * 60))
