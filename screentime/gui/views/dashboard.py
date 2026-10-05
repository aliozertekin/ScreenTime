"""The main dashboard: today's total, a trend, the most-used applications and goal progress.

Everything shown comes from `stats.dashboard_data()` / `goals.evaluate()` (aggregate queries over
`daily_totals`); this view only lays it out and never touches tracking. Native GTK4/libadwaita widgets and
the theme's own colours throughout, so light/dark and custom themes apply automatically.
"""
from __future__ import annotations

import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ... import goals as goals_mod
from ... import stats
from ...db import Database
from ..widgets.app_row import AppUsageRow
from ..widgets.bar_chart import BarChart

PERIOD_LABELS = {1: "Today", 7: "Last 7 days", 30: "Last 30 days"}
TOP_APPS = 8


def _label(text="", css=(), xalign=0.0, wrap=False):
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=wrap)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def _describe(widget, label: str, description: str = ""):
    """Screen-reader text for widgets that are not text themselves (charts, progress bars)."""
    if hasattr(widget, "update_property"):
        props, vals = [Gtk.AccessibleProperty.LABEL], [label]
        if description:
            props.append(Gtk.AccessibleProperty.DESCRIPTION)
            vals.append(description)
        widget.update_property(props, vals)


class DashboardView(Gtk.ScrolledWindow):
    def __init__(self, db: Database):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.db = db
        self.period = 1
        self._signature = None
        self._dynamic: list = []

        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=600)
        self.set_child(clamp)
        self.page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.page, f"set_margin_{side}")(24)
        clamp.set_child(self.page)

        # ---- header: title + period switcher --------------------------------
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.title_label = _label("Today", ["title-1"])
        self.title_label.set_hexpand(True)
        head.append(self.title_label)
        switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, valign=Gtk.Align.CENTER)
        switch.add_css_class("linked")
        self._period_buttons = {}
        group = None
        for days, text in PERIOD_LABELS.items():
            b = Gtk.ToggleButton(label=text)
            if group is None:
                group = b
            else:
                b.set_group(group)
            b.connect("toggled", self._on_period_toggled, days)
            _describe(b, f"Show {text.lower()}")
            switch.append(b)
            self._period_buttons[days] = b
        self._period_buttons[1].set_active(True)
        head.append(switch)
        self.page.append(head)

        # ---- body (rebuilt only when the data changes) ------------------------
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.page.append(self.body)
        self.refresh()

    # ------------------------------------------------------------- events
    def _on_period_toggled(self, button, days):
        if button.get_active() and days != self.period:
            self.period = days
            self.refresh(force=True)

    # ------------------------------------------------------------ building
    def _clear(self):
        child = self.body.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.body.remove(child)
            child = nxt

    def refresh(self, force: bool = False):
        data = stats.dashboard_data(self.db, self.period)
        statuses = goals_mod.evaluate(self.db)
        signature = (self.period, data.start, data.summary.total_seconds,
                     tuple((a.key, a.seconds) for a in data.summary.apps[:TOP_APPS]),
                     tuple(data.daily), data.comparison.previous,
                     tuple((s.key, s.used, s.limit, s.state) for s in statuses))
        if signature == self._signature and not force:
            return                                           # nothing changed: no rebuild, no flicker
        self._signature = signature
        self.title_label.set_label(PERIOD_LABELS[self.period])
        self._clear()
        if not data.has_data:
            self.body.append(self._empty_state())
            return
        self.body.append(self._hero(data))
        if statuses:
            self.body.append(self._goals(statuses))
        self.body.append(self._top_apps(data))
        self.body.append(self._trend(data))

    def _empty_state(self):
        page = Adw.StatusPage(
            icon_name="preferences-system-time-symbolic",
            title="No usage recorded yet",
            description="ScreenTime counts the time an application is in the foreground while you are "
                        "active. Use your computer as usual and your usage will show up here. If nothing "
                        "appears after a while, check that the tracker is running in Settings \u2192 Diagnostics.")
        page.set_vexpand(True)
        return page

    def _hero(self, data: stats.DashboardData):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(inner, f"set_margin_{side}")(18)
        box.append(inner)
        inner.append(_label("Total screen time", ["dim-label"]))
        total = _label(stats.format_duration(data.summary.total_seconds), ["title-1", "accent", "numeric"])
        inner.append(total)
        inner.append(_label(stats.describe_change(data.comparison, data.previous_label), ["dim-label"], wrap=True))
        if data.period_days > 1:
            inner.append(_label(f"Average {stats.format_duration(data.average_seconds)} per day", ["dim-label"]))
        if data.busiest_day:
            day, secs = data.busiest_day
            name = datetime.date.fromisoformat(day).strftime("%A %d %b")
            inner.append(_label(f"Busiest day in the last {len(data.daily)} days: {name}, {stats.format_duration(secs)}",
                                ["dim-label"], wrap=True))
        _describe(box, f"Total screen time {stats.format_duration(data.summary.total_seconds)}")
        return box

    def _goals(self, statuses):
        group = Adw.PreferencesGroup(
            title="Goals today",
            description="Usage, your goal and the warning threshold are shown separately.")
        for s in statuses:
            verdict = {"ok": "On track", "warning": "Close to the limit", "reached": "Goal reached"}[s.state]
            row = Adw.ActionRow(
                title=s.name,
                subtitle=f"{verdict}: {stats.format_duration(s.used)} used of {stats.format_duration(s.limit)} goal "
                         f"(warning at {stats.format_duration(s.warn_at)})")
            bar = Gtk.ProgressBar(fraction=min(1.0, s.fraction), valign=Gtk.Align.CENTER, width_request=140)
            if s.state == "reached":
                bar.add_css_class("error")
            elif s.state == "warning":
                bar.add_css_class("warning")
            _describe(bar, f"{s.name} goal", f"{int(s.fraction * 100)} percent used. {verdict}.")
            row.add_suffix(bar)
            group.add(row)
        return group

    def _top_apps(self, data: stats.DashboardData):
        group = Adw.PreferencesGroup(
            title="Most-used applications",
            description="Share of all tracked time in this period" if data.summary.apps else "")
        if not data.summary.apps:
            row = Adw.ActionRow(title="No application usage in this period")
            row.add_css_class("dim-label")
            group.add(row)
            return group
        top = data.summary.apps[:TOP_APPS]
        biggest = top[0].seconds
        for app in top:
            row = Adw.ActionRow()
            row.set_child(AppUsageRow(app.display_name, app.icon_name, app.seconds, biggest, app.percent))
            row.set_activatable(False)
            _describe(row, f"{app.display_name}: {stats.format_duration(app.seconds)}, {app.percent:.0f} percent")
            group.add(row)
        rest = data.summary.apps[TOP_APPS:]
        if rest:
            more = sum(a.seconds for a in rest)
            row = Adw.ActionRow(
                title=f"{len(rest)} other application{'s' if len(rest) != 1 else ''}",
                subtitle=f"{stats.format_duration(more)} \u00b7 see Applications for the full list")
            row.add_css_class("dim-label")
            group.add(row)
        return group

    def _trend(self, data: stats.DashboardData):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(_label("Daily usage", ["title-4"]))
        chart = BarChart(height=170)
        n = len(data.daily)
        labels = []
        for i, (day, _s) in enumerate(data.daily):
            d = datetime.date.fromisoformat(day)
            labels.append(d.strftime("%a") if n <= 7 else (str(d.day) if (n - 1 - i) % 5 == 0 else ""))
        chart.set_data(labels, [s for _d, s in data.daily], value_fmt=stats.format_duration)
        summary = "; ".join(f"{datetime.date.fromisoformat(d).strftime('%A')} {stats.format_duration(s)}"
                            for d, s in data.daily[-7:])
        _describe(chart, f"Daily usage over the last {n} days", summary)
        box.append(chart)
        return box
