from __future__ import annotations

import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ... import stats
from ...db import Database
from ..widgets.app_row import AppUsageRow
from ..widgets.bar_chart import BarChart

RANGES = ["Today", "Yesterday", "Last 7 days", "This week", "This month", "All time", "Custom"]


class HistoryView(Gtk.Box):
    def __init__(self, db: Database):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.db = db
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_margin_start(24)
        self.set_margin_end(24)

        title = Gtk.Label(label="History", xalign=0)
        title.add_css_class("title-1")
        self.append(title)

        self.range_selector = Gtk.DropDown.new_from_strings(RANGES)
        self.range_selector.set_selected(0)
        self.range_selector.connect("notify::selected", lambda *_: self._on_range_changed())
        self.append(self.range_selector)

        self.custom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.custom_box.set_visible(False)
        today = datetime.date.today()
        self.start_cal = Gtk.Calendar()
        self.end_cal = Gtk.Calendar()
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", lambda *_: self.refresh())
        self.custom_box.append(Gtk.Label(label="From:"))
        self.custom_box.append(self.start_cal)
        self.custom_box.append(Gtk.Label(label="To:"))
        self.custom_box.append(self.end_cal)
        self.custom_box.append(apply_btn)
        self.append(self.custom_box)

        self.total_label = Gtk.Label(xalign=0)
        self.total_label.add_css_class("title-2")
        self.append(self.total_label)

        self.chart = BarChart(height=160)
        self.append(self.chart)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.group = Adw.PreferencesGroup(title="Breakdown by application")
        scroller.set_child(self.group)
        self.append(scroller)

        self._rows = []
        self.refresh()

    def _on_range_changed(self):
        selected = RANGES[self.range_selector.get_selected()]
        self.custom_box.set_visible(selected == "Custom")
        if selected != "Custom":
            self.refresh()

    def _resolve_range(self) -> tuple[str, str]:
        selected = RANGES[self.range_selector.get_selected()]
        today = datetime.date.today()
        if selected == "Today":
            return stats.today_str(), stats.today_str()
        if selected == "Yesterday":
            return stats.yesterday_str(), stats.yesterday_str()
        if selected == "Last 7 days":
            return (today - datetime.timedelta(days=6)).isoformat(), stats.today_str()
        if selected == "This week":
            return stats.week_start_str(), stats.today_str()
        if selected == "This month":
            return stats.month_start_str(), stats.today_str()
        if selected == "All time":
            return "0000-01-01", "9999-12-31"
        # Custom
        gy, gm, gd = self.start_cal.get_date().get_ymd()
        ey, em, ed = self.end_cal.get_date().get_ymd()
        start = datetime.date(gy, gm, gd).isoformat()
        end = datetime.date(ey, em, ed).isoformat()
        if start > end:
            start, end = end, start
        return start, end

    def refresh(self):
        start, end = self._resolve_range()
        summary = stats.usage_in_range(self.db, start, end)
        self.total_label.set_label(f"Total: {stats.format_duration(summary.total_seconds)}")

        daily = stats.daily_totals_all_apps(self.db, start, end)
        labels = [d[5:] for d, _ in daily]
        values = [v / 3600.0 for _, v in daily]  # hours, for a readable axis
        self.chart.set_data(labels, values)

        for row in self._rows:
            self.group.remove(row)
        self._rows = []

        if not summary.apps:
            placeholder = Adw.ActionRow(title="No usage recorded in this range")
            self.group.add(placeholder)
            self._rows.append(placeholder)
            return

        max_seconds = summary.apps[0].seconds
        for app in summary.apps:
            row = Adw.ActionRow()
            usage_row = AppUsageRow(app.display_name, app.icon_name, app.seconds, max_seconds, app.percent)
            row.set_child(usage_row)
            self.group.add(row)
            self._rows.append(row)
