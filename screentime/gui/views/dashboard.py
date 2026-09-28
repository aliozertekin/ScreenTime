from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ... import stats
from ...db import Database
from ..widgets.app_row import AppUsageRow


class DashboardView(Gtk.Box):
    def __init__(self, db: Database):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.db = db
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_margin_start(24)
        self.set_margin_end(24)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="Today", xalign=0)
        title.add_css_class("title-1")
        header.append(title)

        self.total_label = Gtk.Label(xalign=0)
        self.total_label.add_css_class("title-2")
        self.total_label.add_css_class("accent")
        header.append(self.total_label)
        self.append(header)

        self.summary_group = Adw.PreferencesGroup(title="Most used today")
        self.append(self.summary_group)

        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.week_card = self._make_stat_card("This week")
        self.month_card = self._make_stat_card("This month")
        self.alltime_card = self._make_stat_card("All time")
        stats_row.append(self.week_card["box"])
        stats_row.append(self.month_card["box"])
        stats_row.append(self.alltime_card["box"])
        self.append(stats_row)

        self._summary_rows: list[Adw.ActionRow] = []
        self.refresh()

    def _make_stat_card(self, title: str) -> dict:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("card")
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(14)
        inner.set_margin_bottom(14)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("dim-label")
        v = Gtk.Label(xalign=0)
        v.add_css_class("title-2")
        inner.append(t)
        inner.append(v)
        box.append(inner)
        return {"box": box, "value": v}

    def refresh(self):
        today = stats.today_summary(self.db)
        self.total_label.set_label(f"Total: {stats.format_duration(today.total_seconds)}")

        # Adw.PreferencesGroup doesn't expose a bulk-clear API, so remove
        # each row we previously added before rebuilding from fresh data.
        for row in self._summary_rows:
            self.summary_group.remove(row)
        self._summary_rows.clear()

        if not today.apps:
            placeholder = Adw.ActionRow(title="No usage recorded yet today")
            placeholder.add_css_class("dim-label")
            self.summary_group.add(placeholder)
            self._summary_rows.append(placeholder)
        else:
            max_seconds = today.apps[0].seconds
            for app in today.apps[:10]:
                row = Adw.ActionRow()
                usage_row = AppUsageRow(app.display_name, app.icon_name, app.seconds, max_seconds, app.percent)
                row.set_child(usage_row)
                self.summary_group.add(row)
                self._summary_rows.append(row)

        week = stats.week_summary(self.db)
        month = stats.month_summary(self.db)
        alltime = stats.all_time_summary(self.db)
        self.week_card["value"].set_label(stats.format_duration(week.total_seconds))
        self.month_card["value"].set_label(stats.format_duration(month.total_seconds))
        self.alltime_card["value"].set_label(stats.format_duration(alltime.total_seconds))
